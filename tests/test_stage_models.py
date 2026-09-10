import asyncio

import pytest
from fastapi.testclient import TestClient

from deepqueue.agent import model_catalog
from deepqueue.cli import execute, parser
from deepqueue.config import load_settings, private_write
from deepqueue.models import (
    AgentModels,
    Analysis,
    Estimate,
    JobSpec,
    LaunchPlan,
    Resources,
    Settings,
)
from deepqueue.scheduler import Scheduler
from deepqueue.web import create_app


@pytest.mark.parametrize("invalid", ["", "  ", "model\n", "two models", "a\x00b", "x" * 201])
def test_model_ids_are_validated(invalid):
    with pytest.raises(ValueError):
        AgentModels(launch=invalid)


def test_source_model_is_only_for_bound_archives():
    with pytest.raises(ValueError, match="only available for archive"):
        AgentModels(estimate="source-thread")
    with pytest.raises(ValueError, match="requires source_thread_id"):
        JobSpec(
            server="local", cwd="/tmp", command="true", agent_models={"archive": "source-thread"}
        )


@pytest.mark.parametrize("custom", [False, True])
async def test_scheduler_routes_each_new_agent_and_records_its_model(db, custom):
    calls = []

    class RecordingAgent:
        def __init__(self, settings, directory):
            self.model = settings.model

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def analyze(self, prompt, schema, on_thread, on_turn, **kwargs):
            calls.append((schema.__name__, self.model))
            on_thread(f"{self.model}-thread", f"codex://threads/{self.model}-thread")
            on_turn(f"{self.model}-turn")
            if schema is Estimate:
                return Estimate(
                    resources=Resources(ram_mib=128),
                    confidence=1,
                    rationale="CPU fixture",
                    evidence=[],
                    warnings=[],
                    needs_review=False,
                )
            if schema is LaunchPlan:
                return LaunchPlan(command="true", rationale="CPU fixture")
            assert schema is Analysis
            return Analysis(
                summary="Result fixture",
                outcome="success",
                findings=[],
                resource_assessment="",
                next_steps=[],
                suggested_command=None,
            )

    settings = Settings(
        agent_models={
            "estimate": "default-estimate",
            "launch": "default-launch",
            "archive": "default-archive",
        }
    )
    choices = (
        {"estimate": "job-estimate", "launch": "job-launch", "archive": "job-archive"}
        if custom
        else {}
    )
    job = db.submit(JobSpec(server="local", cwd=str(db.home), command="true", agent_models=choices))
    scheduler = Scheduler(db.home, settings, agent_factory=RecordingAgent)

    async def advance():
        scheduler.start_agents()
        await asyncio.gather(*scheduler.agents.values())

    await advance()
    assert db.job(job["id"])["status"] == "queued"
    assert db.reserve(job["id"], [])
    await advance()
    assert db.job(job["id"])["launch_status"] == "completed"
    db.finish(job["id"], {"status": "succeeded", "exit_code": 0})
    await advance()
    assert db.job(job["id"])["archive_status"] == "completed"
    prefix = "job" if custom else "default"
    assert calls == [
        ("Estimate", f"{prefix}-estimate"),
        ("LaunchPlan", f"{prefix}-launch"),
        ("Analysis", f"{prefix}-archive"),
    ]
    assert [agent["model"] for agent in db.detail(job["id"])["agents"]] == [
        model for _, model in calls
    ]


def test_cli_batch_stage_overrides_preserve_other_stages_and_source(db, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "stage-source")
    manifest = {
        "name": "stage-models",
        "defaults": {
            "server": "local",
            "cwd": str(db.home),
            "command": "true",
            "agent_models": {"estimate": "shared-estimate", "launch": "shared-launch"},
        },
        "experiments": [
            {"key": "one", "agent_models": {"estimate": "specific-estimate"}},
            {"key": "two"},
        ],
    }
    import json

    private_write(db.home / "batch.json", json.dumps(manifest))
    flags = [
        str(db.home / "batch.json"),
        "--from-agent",
        "--archive-model",
        "source-thread",
        "--launch-model",
        "flag-launch",
    ]
    preview = execute(parser().parse_args(["--home", str(db.home), "batch", "preview", *flags]))
    assert not db.jobs()
    assert [job["agent_models"] for job in preview["experiments"]] == [
        {"estimate": estimate, "launch": "flag-launch", "archive": "source-thread"}
        for estimate in ("specific-estimate", "shared-estimate")
    ]
    submitted = execute(parser().parse_args(["--home", str(db.home), "batch", "submit", *flags]))
    repeated = execute(parser().parse_args(["--home", str(db.home), "batch", "submit", *flags]))
    assert submitted["jobs"] == repeated["jobs"]
    assert all(job["spec"]["source_thread_id"] == "stage-source" for job in db.jobs())


async def test_catalog_uses_each_connection_once_and_does_not_infer(monkeypatch):
    calls = []

    class CatalogAgent:
        def __init__(self, settings, directory):
            self.socket = settings.agent_socket

        async def __aenter__(self):
            calls.append(self.socket)
            return self

        async def __aexit__(self, *_):
            pass

        async def models(self):
            if self.socket == "/unavailable":
                raise ConnectionError("offline fixture")
            return [
                {
                    "id": "catalog-id",
                    "model": "provider/model",
                    "displayName": "Provider model",
                    "defaultReasoningEffort": "high",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low"},
                        {"reasoningEffort": "high"},
                    ],
                }
            ]

    monkeypatch.setattr("deepqueue.agent.AppServer", CatalogAgent)
    result = await model_catalog(Settings(agent_socket="auto", return_agent_socket="/unavailable"))
    assert calls == ["auto", "/unavailable"]
    assert (
        result["models"]["estimate"]
        == result["models"]["launch"]
        == [
            {
                "model": "provider/model",
                "name": "Provider model",
                "hidden": False,
                "default_effort": "high",
                "efforts": ["low", "high"],
            }
        ]
    )
    assert result["models"]["archive"] == []
    assert result["errors"] == {"archive": "offline fixture"}


def test_web_exposes_selections_and_defaults_with_a_source_choice(db, monkeypatch):
    settings = load_settings(db.home)
    settings.agent_models.launch = "default-launch"
    settings.agent_models.archive = "default-archive"
    private_write(db.home / "config.json", settings.model_dump_json())
    response_catalog = {"models": {"estimate": [{"model": "web-estimate"}]}, "errors": {}}

    async def catalog(_settings):
        return response_catalog

    monkeypatch.setattr("deepqueue.web.model_catalog", catalog)
    with TestClient(
        create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"}
    ) as web:
        assert web.get("/api/models").json() == response_catalog
        assert web.get("/api/state").json()["agent_models"] == {
            "launch": "default-launch",
            "archive": "default-archive",
        }
        response = web.post(
            "/api/jobs",
            json={
                "server": "local",
                "cwd": str(db.home),
                "command": "true",
                "source_thread_id": "web-source",
                "agent_models": {"estimate": "web-estimate", "archive": "source-thread"},
            },
        )
        assert response.status_code == 200
        detail = web.get(f"/api/jobs/{response.json()['id']}").json()
        assert detail["effective_agent_models"] == {
            "estimate": "web-estimate",
            "launch": "default-launch",
            "archive": "source-thread",
        }
        assert detail["spec"]["agent_models"]["archive"] == "source-thread"
