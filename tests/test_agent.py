import sys
from pathlib import Path

import pytest

from deepqueue.agent import AppServer
from deepqueue.config import Secrets, load_settings
from deepqueue.models import Estimate, JobSpec, Resources, Settings
from deepqueue.scheduler import Scheduler


async def test_protocol_structured_answer_and_thread_callbacks(tmp_path):
    settings = Settings(
        agent_command=[sys.executable, str(Path(__file__).with_name("fake_app_server.py"))]
    )
    links, turns = [], []
    async with AppServer(settings, tmp_path / "agent") as agent:
        result = await agent.analyze(
            "CPU fixture", Estimate, lambda *x: links.append(x), turns.append
        )
    assert result.confidence == 0.95
    assert links == [("fixture-thread", "codex://threads/fixture-thread")]
    assert turns == ["fixture-turn"]
    assert (tmp_path / "agent" / "response.json").exists()


async def test_failed_analysis_preserves_thread_link_and_command_does_not_run(db):
    class BrokenAgent:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def analyze(self, prompt, schema, on_thread, on_turn):
            on_thread("recorded-before-failure", "codex://threads/recorded-before-failure")
            on_turn("turn")
            raise RuntimeError("fixture disconnect")

    settings = load_settings(db.home).model_copy(update={"agent_max_attempts": 1})
    scheduler = Scheduler(db.home, settings, agent_factory=BrokenAgent)
    job = db.submit(JobSpec(server="local", cwd=str(db.home), command="true"))
    agent_id = db.start_agent(job["id"], "estimate", settings.model)
    await scheduler.analyze(job, "estimate", agent_id)
    detail = db.detail(job["id"])
    assert detail["status"] == "failed"
    assert not detail["runs"]
    assert detail["agents"][0]["thread_id"] == "recorded-before-failure"


async def test_explicit_resources_are_not_lowered_or_silently_expanded(db):
    class Agent:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def analyze(self, *_):
            return Estimate(
                resources=Resources(gpu_count=2, gpu_memory_mib=20000),
                confidence=0.99,
                rationale="Needs two GPUs",
                evidence=[],
                warnings=[],
                needs_review=False,
            )

    settings = load_settings(db.home)
    scheduler = Scheduler(db.home, settings, agent_factory=Agent)
    explicit = Resources(gpu_count=1, gpu_ids=["4"], gpu_memory_mib=12000)
    job = db.submit(JobSpec(server="local", cwd=str(db.home), command="true", resources=explicit))
    agent_id = db.start_agent(job["id"], "estimate", settings.model)
    await scheduler.analyze(job, "estimate", agent_id)
    assert db.job(job["id"])["status"] == "queued"
    assert db.job(job["id"])["resources"] == explicit.model_dump()


def test_credentials_are_encrypted_and_environment_references_work(db, monkeypatch):
    secrets = Secrets(db.home)
    reference = secrets.put("fixture-only-password")
    assert secrets.get(reference) == "fixture-only-password"
    payload = (db.home / "secrets" / reference.split(":")[1]).read_bytes()
    assert b"fixture-only-password" not in payload
    assert (db.home / "secrets.key").stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("DEEPQUEUE_TEST_SECRET", "fixture-environment")
    assert secrets.get("env:DEEPQUEUE_TEST_SECRET") == "fixture-environment"
    with pytest.raises(ValueError):
        secrets.get("vault:../../etc/passwd")
