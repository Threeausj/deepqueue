import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from deepqueue.cli import execute, parser
from deepqueue.config import load_settings, private_write, update_settings
from deepqueue.db import SCHEMA_VERSION
from deepqueue.models import AgentEfforts, Estimate, JobSpec, Resources, Settings
from deepqueue.scheduler import Scheduler
from deepqueue.web import create_app


def job_spec(db, **fields):
    return JobSpec(server="local", cwd=str(db.home), command="true", **fields)


def test_global_settings_save_is_validated_and_preserves_other_config(db):
    update_settings(db.home, {"agent_timeout_seconds": 99, "agent_socket": "/fixture/socket"})
    with TestClient(
        create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"}
    ) as web:
        saved = web.post(
            "/api/settings",
            json={
                "agent_models": {"launch": "configured-launch", "archive": "source-thread"},
                "agent_efforts": {"estimate": "low", "launch": "high", "archive": "source-thread"},
            },
        )
        assert saved.status_code == 200
        assert web.get("/api/settings").json() == saved.json()
        assert load_settings(db.home).agent_timeout_seconds == 99
        assert load_settings(db.home).agent_socket == "/fixture/socket"
        assert web.get("/api/state").json()["agent_efforts"]["launch"] == "high"
        submitted = web.post(
            "/api/jobs",
            json={
                "server": "local",
                "cwd": str(db.home),
                "command": "true",
                "source_thread_id": "test-source",
            },
        ).json()
        assert not any(submitted["spec"]["agent_models"].values())
        assert not any(submitted["spec"]["agent_efforts"].values())
        assert submitted["effective_agent_models"]["launch"] == "configured-launch"
        assert submitted["effective_agent_efforts"] == {
            "estimate": "low",
            "launch": "high",
            "archive": "source-thread",
        }
        before = (db.home / "config.json").read_bytes()
        for invalid in [
            {"agent_models": {}, "agent_efforts": {"launch": "source-thread"}},
            {"agent_models": {}, "agent_efforts": {"estimate": "bad value"}},
            {"agent_models": {}, "agent_efforts": {}, "agent_command": ["unrelated"]},
        ]:
            assert web.post("/api/settings", json=invalid).status_code == 422
            assert (db.home / "config.json").read_bytes() == before
        assert (
            web.post(
                "/api/settings", headers={"Origin": "https://other.example"}, json={}
            ).status_code
            == 403
        )


def test_concurrent_config_edits_preserve_unrelated_changes(db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(update_settings, db.home, changes)
            for changes in [
                {"agent_models": {"launch": "configured-launch"}},
                {"agent_efforts": {"estimate": "high"}},
            ]
        ]
        for future in futures:
            future.result()
    settings = load_settings(db.home)
    assert settings.agent_models.launch == "configured-launch"
    assert settings.agent_efforts.estimate == "high"


async def test_reload_applies_only_to_future_agents_and_hot_settings(db):
    calls = []

    class Recorder:
        def __init__(self, settings, _directory):
            self.settings = settings

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def analyze(self, prompt, schema, on_thread, on_turn, **kwargs):
            calls.append((self.settings.model, self.settings.agent_effort))
            return Estimate(
                resources=Resources(),
                confidence=1,
                rationale="CPU fixture",
                evidence=[],
                warnings=[],
                needs_review=False,
            )

    settings = update_settings(
        db.home, {"model": "before-model", "agent_effort": "low", "max_agents": 1}
    )
    scheduler = Scheduler(db.home, settings, agent_factory=Recorder)
    first = db.submit(job_spec(db))
    scheduler.start_agents()
    update_settings(db.home, {"model": "after-model", "agent_effort": "high", "poll_seconds": 20})
    scheduler.reload_agent_settings()
    assert scheduler.settings.poll_seconds == settings.poll_seconds
    await asyncio.gather(*scheduler.agents.values())
    second = db.submit(job_spec(db))
    scheduler.start_agents()
    await asyncio.gather(*scheduler.agents.values())
    assert calls == [("before-model", "low"), ("after-model", "high")]
    assert db.last_agent(first["id"], "estimate")["effort"] == "low"
    assert db.last_agent(second["id"], "estimate")["effort"] == "high"
    private_write(db.home / "config.json", "invalid JSON")
    scheduler.reload_agent_settings()
    assert scheduler.settings.agent_effort == "high"


def test_effort_cli_flags_and_improvement_inheritance(db, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "test-source")
    manifest = {
        "name": "effort-fixture",
        "defaults": {
            "server": "local",
            "cwd": str(db.home),
            "command": "true",
            "agent_efforts": {"estimate": "low", "launch": "high"},
        },
        "experiments": [{"key": "one", "agent_efforts": {"estimate": "medium"}}, {"key": "two"}],
    }
    private_write(db.home / "batch.json", json.dumps(manifest))
    flags = [
        str(db.home / "batch.json"),
        "--from-agent",
        "--launch-effort",
        "medium",
        "--archive-effort",
        "source-thread",
    ]
    preview = execute(parser().parse_args(["--home", str(db.home), "batch", "preview", *flags]))
    assert [job["agent_efforts"] for job in preview["experiments"]] == [
        {"estimate": level, "launch": "medium", "archive": "source-thread"}
        for level in ("medium", "low")
    ]
    parent = db.submit(
        job_spec(
            db,
            resources=Resources(),
            skip_estimate=True,
            source_thread_id="test-source",
            completion_mode="improve",
            agent_efforts={"estimate": "low", "launch": "high", "archive": "source-thread"},
        )
    )
    db.reserve(parent["id"], [])
    db.finish(parent["id"], {"status": "succeeded"})
    child_spec = job_spec(
        db,
        source_thread_id="test-source",
        parent_job_id=parent["id"],
        agent_efforts={"launch": "medium"},
    )
    child = db.submit(child_spec)
    assert child["spec"]["agent_efforts"] == {
        "estimate": "low",
        "launch": "medium",
        "archive": "source-thread",
    }
    assert db.submit(child_spec)["id"] == child["id"]
    result = execute(
        parser().parse_args(
            ["--home", str(db.home), "config", "set", "agent_efforts", '{"launch":"high"}']
        )
    )
    assert not result["restart_daemon_to_apply"]


def test_v4_migration_preserves_history_without_inventing_effort(db):
    job = db.submit(job_spec(db))
    agent = db.start_agent(job["id"], "estimate", "old-model")
    with db.connection(write=True) as con:
        con.execute("ALTER TABLE agent_runs DROP COLUMN effort")
        con.execute("PRAGMA user_version=4")
    db.ensure_schema()
    assert db.schema_version() == SCHEMA_VERSION
    assert db.last_agent(job["id"], "estimate")["id"] == agent
    assert db.last_agent(job["id"], "estimate")["effort"] is None


@pytest.mark.parametrize("value", ["", "High", "two words", "a\x00b"])
def test_effort_values_are_validated(value):
    with pytest.raises(ValueError):
        AgentEfforts(estimate=value)
    with pytest.raises(ValueError):
        Settings(agent_effort=value)
