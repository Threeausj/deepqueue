import json

import pytest
from fastapi.testclient import TestClient

from deepqueue.access import Access
from deepqueue.cli import execute, parser
from deepqueue.config import private_write
from deepqueue.db import Database
from deepqueue.models import Estimate, JobSpec, JobUpdate, Resources, Server, Settings
from deepqueue.scheduler import Scheduler, launch_failure_kind
from deepqueue.web import create_app


def spec(db, **changes):
    return JobSpec.model_validate(
        {
            "server": "local",
            "cwd": str(db.home),
            "command": "true",
            "source_thread_id": "source",
            "resources": {"ram_mib": 128},
            "skip_estimate": True,
            **changes,
        }
    )


def cli(db, *args):
    return execute(parser().parse_args(["--home", str(db.home), "job", *args]))


def completed(db, **changes):
    job = db.submit(spec(db, **changes))
    db.reserve(job["id"], [])
    db.finish(job["id"], {"status": "succeeded", "exit_code": 0})
    return db.job(job["id"])


def test_running_finish_job_can_switch_to_improvement_and_preserve_execution(db):
    original = spec(db, idempotency_key="runtime-control", agent_models={"launch": "launch-old"})
    job = db.submit(original)
    run = db.reserve(job["id"], [])
    db.running(job["id"])
    before = db.run(run)
    cli(db, "mode", job["id"], "improve", "--max-improvement-rounds", "2")
    cli(
        db,
        "update",
        job["id"],
        "--archive-model",
        "source-thread",
        "--archive-effort",
        "high",
        "--intent",
        "Compare validation accuracy with the previous experiment",
    )
    updated = Database(db.home).job(job["id"])
    assert updated["completion_mode"] == updated["spec"]["completion_mode"] == "improve"
    assert updated["spec"]["max_improvement_rounds"] == 2
    assert updated["spec"]["agent_models"]["launch"] == "launch-old"
    assert updated["spec"]["agent_efforts"]["archive"] == "high"
    assert db.run(run) == before
    assert db.submit(original)["id"] == job["id"]
    db.finish(job["id"], {"status": "succeeded"})
    assert db.held_run(job["id"])
    child = db.submit(spec(db, parent_job_id=job["id"]))
    assert child["priority"] == 100
    assert child["completion_mode"] == "improve"
    assert child["spec"]["max_improvement_rounds"] == 2
    assert child["spec"]["agent_efforts"]["archive"] == "high"
    with pytest.raises(ValueError, match="child"):
        cli(db, "mode", job["id"], "improve", "--max-improvement-rounds", "3")
    cli(db, "mode", job["id"], "finish")
    assert not db.held_run(job["id"])
    assert db.job(child["id"])["status"] == "cancelled"
    assert db.job(child["id"])["spec"]["completion_mode"] == "finish"


def test_update_revalidates_preparation_and_cannot_change_an_admitted_command(db):
    job = db.submit(spec(db, skip_estimate=False))
    agent = db.start_agent(job["id"], "estimate", "fixture")
    with pytest.raises(ValueError, match="before estimation"):
        db.update_job(job["id"], {"command": "echo changed"})
    db.complete_agent(agent, {"rationale": "old estimate"}, "queued", Resources().model_dump())
    db.update_job(job["id"], {"command": "echo changed", "resources": {"gpu_count": 2}})
    updated = db.job(job["id"])
    assert updated["status"] == "pending" and updated["estimate"] is None
    assert updated["resources"]["gpu_count"] == 2
    assert db.reserve(job["id"], []) is None
    other = db.submit(spec(db))
    db.reserve(other["id"], [])
    before = db.detail(other["id"])
    with pytest.raises(ValueError, match="before estimation"):
        db.update_job(other["id"], {"completion_mode": "improve", "command": "echo changed"})
    assert db.detail(other["id"]) == before


def test_partial_settings_merge_and_explicit_null_resets_only_one_stage(db):
    job = db.submit(spec(db, agent_models={"estimate": "a", "launch": "b"}))
    cli(db, "update", job["id"], "--launch-max-retries", "0", "--archive-model", "source-thread")
    db.update_job(job["id"], {"agent_models": {"launch": None}, "launch_max_retries": None})
    current = db.job(job["id"])
    assert current["spec"]["agent_models"] == {
        "estimate": "a",
        "launch": None,
        "archive": "source-thread",
    }
    assert current["spec"]["launch_max_retries"] is None
    for invalid in ({"command": None}, {"server": "elsewhere"}, {"max_improvement_rounds": 0}):
        with pytest.raises(ValueError):
            db.update_job(job["id"], invalid)


def test_metadata_updates_preserve_legacy_execution_opt_outs(db):
    job = db.submit(spec(db))
    legacy = dict(job["spec"])
    legacy.pop("launch_agent")
    legacy.pop("tmux")
    with db.connection(write=True) as con:
        con.execute("UPDATE jobs SET spec=? WHERE id=?", (json.dumps(legacy), job["id"]))
    db.update_job(job["id"], {"intent": "Compare final accuracy"})
    saved = db.job(job["id"])["spec"]
    assert "launch_agent" not in saved and "tmux" not in saved
    db.reserve(job["id"], [])
    assert db.job(job["id"])["launch_status"] == "skipped"


def test_mode_can_change_after_exit_before_archive_but_does_not_replay_a_sent_callback(db):
    job = completed(db)
    cli(db, "mode", job["id"], "improve")
    agent = db.start_agent(job["id"], "archive", "source-thread")
    cli(db, "mode", job["id"], "finish")
    with pytest.raises(ValueError, match="before archive"):
        cli(db, "mode", job["id"], "improve")
    db.complete_agent(agent, {"summary": "finished"})
    with pytest.raises(ValueError, match="before archive"):
        cli(db, "mode", job["id"], "improve")
    assert len(db.detail(job["id"])["agents"]) == 1


def test_deferred_sent_archive_requires_a_new_delivery_before_changing_to_improve(db):
    job = completed(db)
    agent = db.start_agent(job["id"], "archive", "source-thread")
    previous_delivery = db.agent_delivery(agent)["delivery_id"]
    db.agent_dispatched(agent)
    db.defer_agent(agent, "Connection interrupted after dispatch")
    with pytest.raises(ValueError, match="already sent"):
        cli(db, "mode", job["id"], "improve")
    assert db.job(job["id"])["completion_mode"] == "finish"
    cli(db, "retry-agent", job["id"], "--phase", "archive")
    cli(db, "mode", job["id"], "improve")
    retry = db.start_agent(job["id"], "archive", "source-thread")
    assert db.agent_delivery(retry)["delivery_id"] != previous_delivery


def test_scoped_remote_settings_api_enforces_ownership_and_round_limit(db, monkeypatch):
    db.add_server(Server(name="other"))
    access = Access(db.home)
    access.create("admin")
    credential = access.create("local", "local")
    headers = {"Authorization": "Bearer " + credential["token"], "X-DeepQueue": "1"}
    job = db.submit(spec(db))
    foreign = db.submit(spec(db, server="other"))
    with TestClient(create_app(db.home), base_url="http://localhost", headers=headers) as web:

        def request(_client, path, data=None, **query):
            result = (
                web.post("/api" + path, json=data) if data is not None else web.get("/api" + path)
            )
            assert result.status_code == 200, result.text
            return result.json()

        monkeypatch.setattr("deepqueue.remote.Client.request", request)
        flags = ["--url", "http://localhost", "--target-server", "local", "job"]
        updated = execute(
            parser().parse_args(
                [
                    *flags,
                    "update",
                    job["id"],
                    "--completion-mode",
                    "improve",
                    "--max-improvement-rounds",
                    "2",
                    "--archive-effort",
                    "high",
                ]
            )
        )
        assert updated["spec"]["max_improvement_rounds"] == 2
        assert updated["spec"]["agent_efforts"]["archive"] == "high"
        result = execute(
            parser().parse_args(
                [
                    *flags,
                    "mode",
                    job["id"],
                    "improve",
                    "--max-improvement-rounds",
                    "3",
                ]
            )
        )
        assert result["spec"]["max_improvement_rounds"] == 3
        assert (
            web.post(f"/api/jobs/{foreign['id']}/settings", json={"intent": "no"}).status_code
            == 403
        )
        assert (
            web.post(f"/api/jobs/{job['id']}/settings", json={"server": "other"}).status_code == 422
        )
        assert db.job(foreign["id"])["spec"]["intent"] == ""


async def test_advisory_estimate_forwards_low_confidence_code_issues_to_launch(db):
    issue = "train.py uses physical device 7; use the assigned logical index"

    class Estimator:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def analyze(self, *_):
            return Estimate(
                resources=Resources(gpu_count=2),
                confidence=0.1,
                rationale="Approximate request",
                evidence=[],
                warnings=["Unknown peak memory"],
                code_issues=[issue],
                needs_review=True,
            )

    job = db.submit(spec(db, skip_estimate=False, resources={"gpu_count": 1}))
    scheduler = Scheduler(db.home, Settings(), agent_factory=Estimator)
    agent = db.start_agent(job["id"], "estimate", "fixture")
    await scheduler.analyze(job, "estimate", agent)
    assert db.job(job["id"])["status"] == "queued"
    db.reserve(job["id"], [{"index": 3, "uuid": "GPU-3", "name": "fixture"}])
    data = await scheduler.evidence(db.job(job["id"]), "launch")
    assert data["estimate"]["code_issues"] == [issue]
    assert data["approved_resources"]["gpu_count"] == 1
    assert data["logical_gpu_ids"] == [0]


def test_failed_estimate_falls_back_only_to_an_explicit_request_and_can_retry(db):
    explicit = db.submit(spec(db, skip_estimate=False, resources={"gpu_count": 4}))
    unknown = db.submit(spec(db, skip_estimate=False, resources=None))
    for job in (explicit, unknown):
        agent = db.start_agent(job["id"], "estimate", "fixture")
        db.fail_agent(agent, "model unavailable", 1)
    assert db.job(explicit["id"])["status"] == "queued"
    assert db.job(explicit["id"])["resources"]["gpu_count"] == 4
    assert db.job(unknown["id"])["status"] == "failed"
    cli(db, "retry-agent", unknown["id"], "--phase", "estimate")
    assert db.job(unknown["id"])["status"] == "pending"


async def test_archive_uses_previous_snapshot_instead_of_overwritten_artifacts(db):
    parent = completed(db, completion_mode="improve", artifact_files=["metrics.json"])
    old_agent = db.start_agent(parent["id"], "archive", "fixture")
    saved = {
        "job_id": parent["id"],
        "log_tail": {"text": "epoch=3 val_acc=0.70"},
        "artifacts": {"files": [{"path": "metrics.json", "text": '{"val_acc": 0.70}'}]},
    }
    private_write(db.home / "agents" / old_agent / "evidence.json", json.dumps(saved))
    db.complete_agent(old_agent, {"summary": "Previous validation accuracy: 70%"})
    private_write(db.home / "metrics.json", '{"val_acc": 0.75}')
    child = completed(db, parent_job_id=parent["id"], artifact_files=["metrics.json"])
    data = await Scheduler(db.home, Settings()).evidence(child, "archive")
    assert data["comparison"]["job_id"] == parent["id"]
    assert data["comparison"]["selection"] == "parent"
    assert data["comparison"]["artifacts"] == saved["artifacts"]
    assert "0.75" in json.dumps(data["artifacts"])


async def test_comparison_selection_excludes_other_projects_servers_and_later_runs(db):
    previous = completed(db)
    completed(db, cwd=str(db.home / "other-project"))
    db.add_server(Server(name="other"))
    completed(db, server="other")
    current = db.submit(spec(db))
    db.reserve(current["id"], [])
    completed(db)  # Finishes after the current run starts; not its previous experiment.
    db.finish(current["id"], {"status": "succeeded"})
    current = db.job(current["id"])
    baseline, selection = db.comparison_job(current)
    assert baseline["id"] == previous["id"] and selection == "previous_same_project"
    evidence = await Scheduler(db.home, Settings()).evidence(current, "archive")
    assert evidence["comparison"]["artifacts"]["unavailable"]
    explicit = completed(db, cwd=str(db.home / "named-baseline"))
    db.update_job(current["id"], JobUpdate(baseline_job_id=explicit["id"]))
    assert db.comparison_job(db.job(current["id"]))[0]["id"] == explicit["id"]


@pytest.mark.parametrize(
    "text",
    [
        "train.py: error: unrecognized arguments: --batchsize 8",
        "RuntimeError: Default process group has not been initialized",
        "ValueError: environment variable RANK expected, but not set",
    ],
)
def test_supported_startup_configuration_errors_are_recoverable(text):
    assert launch_failure_kind({"status": "failed"}, text) == "launch_config"
    assert launch_failure_kind({"status": "succeeded"}, text) is None


def test_runtime_retry_budget_update_takes_effect_for_the_next_recovery(db):
    job = db.submit(spec(db, resources={"gpu_count": 1}))
    run = db.reserve(job["id"], [{"index": 3, "uuid": "GPU-3", "name": "fixture"}])
    db.update_job(job["id"], {"launch_max_retries": 0})
    assert db.retry_launch(job["id"], run, {"status": "failed"}, "launch_config", 3) is None
    db.update_job(job["id"], {"launch_max_retries": 1})
    retry = db.retry_launch(job["id"], run, {"status": "failed"}, "launch_config", 0)
    assert retry and db.run(retry)["recovery"]["kind"] == "launch_config"
    assert db.retry_launch(job["id"], retry, {"status": "failed"}, "launch_config", 10) is None


async def test_archive_callback_uses_the_latest_completion_policy(db):
    job = completed(db)
    stale = db.job(job["id"])
    cli(db, "mode", job["id"], "improve", "--max-improvement-rounds", "2")
    prompts = []

    class Feedback:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def return_to_thread(self, prompt, *args, **kwargs):
            prompts.append(prompt)
            return "No measured training metrics are available; no justified child experiment."

    agent = db.start_agent(job["id"], "archive", "source-thread")
    await Scheduler(db.home, Settings(), agent_factory=Feedback).analyze(stale, "archive", agent)
    assert db.job(job["id"])["archive_status"] == "completed"
    data = json.loads(prompts[0].split("Evidence JSON:\n", 1)[1])
    assert data["completion_mode"] == data["job"]["completion_mode"] == "improve"
    assert data["job"]["max_improvement_rounds"] == 2
    assert "CONTINUE IMPROVING" in prompts[0]
    assert not db.improvement_child(job["id"])


def test_retrying_failed_preparation_reopens_archive_but_never_retries_executed_training(db):
    job = db.submit(spec(db))
    db.reserve(job["id"], [])
    launch = db.start_agent(job["id"], "launch", "fixture")
    db.fail_agent(launch, "invalid plan", 1)
    archive = db.start_agent(job["id"], "archive", "source-thread")
    with pytest.raises(ValueError, match="active archive"):
        cli(db, "retry-agent", job["id"], "--phase", "launch")
    db.complete_agent(archive, {"summary": "Preparation failed"})
    cli(db, "retry-agent", job["id"], "--phase", "launch")
    current = db.job(job["id"])
    assert current["status"] == "queued" and current["archive_status"] == "pending"
    assert current["analysis"] is None and current["run_id"] is None
    run = db.reserve(job["id"], [])
    db.finish(job["id"], {"status": "failed", "exit_code": 1})
    with db.connection(write=True) as con:
        con.execute("UPDATE jobs SET launch_status='failed' WHERE id=?", (job["id"],))
    with pytest.raises(ValueError, match="did not execute"):
        cli(db, "retry-agent", job["id"], "--phase", "launch")
    assert db.job(job["id"])["run_id"] == run
