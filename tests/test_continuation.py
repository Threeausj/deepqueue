import asyncio
import json
import shlex
import subprocess
import sys
import time

import pytest

from deepqueue.cli import bind_origin, execute, parser
from deepqueue.db import SCHEMA_VERSION, Database
from deepqueue.models import JobSpec, LaunchPlan, Server, Settings
from deepqueue.scheduler import Scheduler
from deepqueue.transport import Transport


def spec(db, **updates):
    return JobSpec.model_validate(
        {
            "server": "local",
            "cwd": str(db.home),
            "command": "true",
            "resources": {"ram_mib": 128},
            "skip_estimate": True,
            "source_thread_id": "test-source",
            **updates,
        }
    )


def finish_parent(db, **updates):
    job = db.submit(spec(db, completion_mode="improve", **updates))
    db.reserve(job["id"], [])
    db.finish(job["id"], {"status": "succeeded"})
    return db.job(job["id"])


@pytest.mark.parametrize("limit", [1, 3])
def test_bounded_idempotent_improvement_and_inherited_priority(db, limit):
    parent = finish_parent(db, max_improvement_rounds=limit)
    first = parent
    for round_number in range(1, limit + 1):
        request = spec(db, parent_job_id=parent["id"], command=f"echo round-{round_number}")
        child = db.submit(request)
        assert db.submit(request)["id"] == child["id"]
        assert child["priority"] == 100
        assert child["spec"]["improvement_round"] == round_number
        assert child["spec"]["max_improvement_rounds"] == limit
        assert child["completion_mode"] == ("improve" if round_number < limit else "finish")
        with pytest.raises(ValueError, match="Idempotency"):
            db.submit(request.model_copy(update={"command": "different"}))
        db.reserve(child["id"], [])
        assert not db.held_run(parent["id"])
        db.finish(child["id"], {"status": "succeeded"})
        parent = child
    with pytest.raises(ValueError, match="authorized"):
        db.submit(spec(db, parent_job_id=parent["id"]))
    assert len(db.jobs()) == limit + 1
    assert db.detail(first["id"])["improvement_child"]
    assert not db.active_runs("local")


def test_improvement_inherits_stage_models_and_allows_targeted_override(db):
    parent = finish_parent(
        db,
        agent_models={
            "estimate": "parent-estimate",
            "launch": "parent-launch",
            "archive": "source-thread",
        },
    )
    request = spec(db, parent_job_id=parent["id"], agent_models={"launch": "child-launch"})
    child = db.submit(request)
    assert child["spec"]["agent_models"] == {
        "estimate": "parent-estimate",
        "launch": "child-launch",
        "archive": "source-thread",
    }
    assert db.submit(request)["id"] == child["id"]
    other = finish_parent(db, agent_models={"launch": "parent-launch", "archive": "source-thread"})
    reset = db.submit(spec(db, parent_job_id=other["id"], agent_models={"launch": None}))
    assert reset["spec"]["agent_models"] == {
        "estimate": None,
        "launch": None,
        "archive": "source-thread",
    }


def test_improvement_cli_defaults_and_validation(db, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "test-source")
    args = parser().parse_args(
        [
            "job",
            "submit",
            "--from-agent",
            "--completion-mode",
            "improve",
            "--max-improvement-rounds",
            "2",
        ]
    )
    submitted = db.submit(
        JobSpec.model_validate(
            bind_origin({"server": "local", "cwd": str(db.home), "command": "true"}, args)
        )
    )
    assert submitted["completion_mode"] == "improve"
    assert submitted["spec"]["max_improvement_rounds"] == 2
    assert JobSpec(server="local", cwd="/tmp", command="true").max_improvement_rounds == 1
    with pytest.raises(ValueError, match="source"):
        JobSpec(server="local", cwd="/tmp", command="true", completion_mode="improve")
    parent = finish_parent(db)
    with pytest.raises(ValueError, match="source conversation"):
        db.submit(spec(db, parent_job_id=parent["id"], source_thread_id="other"))
    execute(parser().parse_args(["--home", str(db.home), "job", "mode", parent["id"], "finish"]))
    with pytest.raises(ValueError, match="authorized"):
        db.submit(spec(db, parent_job_id=parent["id"]))
    assert not db.held_run(parent["id"])


@pytest.mark.parametrize("reason", ["expiry", "failed", "no-child"])
def test_holds_release_when_continuation_cannot_proceed(db, reason):
    parent = finish_parent(db)
    assert db.active_runs("local")
    if reason == "expiry":
        with db.connection(write=True) as con:
            con.execute("UPDATE resource_holds SET expires_at=0")
    else:
        agent = db.start_agent(parent["id"], "archive", "source")
        if reason == "failed":
            db.fail_agent(agent, "unavailable", 1)
        else:
            db.complete_agent(agent, {"summary": "No justified change"})
    db.release_expired_holds()
    assert not db.held_run(parent["id"])
    assert not db.active_runs("local")


def test_held_gpu_transfer_is_atomic_and_child_precedes_ordinary_work(db):
    gpu0, gpu1 = [{"index": i, "uuid": f"GPU-{i}", "name": "fixture"} for i in range(2)]
    parent = db.submit(spec(db, completion_mode="improve", resources={"gpu_count": 1}))
    parent_run = db.reserve(parent["id"], [gpu0])
    db.finish(parent["id"], {"status": "succeeded"})
    other = db.submit(spec(db, resources={"gpu_count": 1}, priority=100))
    db.reserve(other["id"], [gpu1])
    child = db.submit(spec(db, parent_job_id=parent["id"], resources={"gpu_count": 2}))
    assert db.reserve(child["id"], [gpu0, gpu1]) is None
    assert db.held_run(parent["id"])["id"] == parent_run
    assert len(db.leases("local")) == 2
    db.finish(other["id"], {"status": "succeeded"})
    run = db.reserve(child["id"], [gpu0, gpu1])
    assert {lease["run_id"] for lease in db.leases("local")} == {run}
    assert not db.held_run(parent["id"])
    scheduler = Scheduler(db.home, Settings())
    ordinary = db.submit(spec(db, priority=100))
    assert scheduler.queue_order([ordinary, child])[0]["id"] == child["id"]


@pytest.mark.parametrize("status", ["queued", "starting", "running"])
def test_finish_stops_descendants_without_stopping_running_training(db, status):
    parent = finish_parent(db, max_improvement_rounds=3)
    child = db.submit(spec(db, parent_job_id=parent["id"]))
    if status != "queued":
        db.reserve(child["id"], [])
    if status == "running":
        db.running(child["id"])
    db.set_completion_mode(parent["id"], "finish")
    current = db.job(child["id"])
    assert current["completion_mode"] == "finish"
    assert bool(current["cancel_requested"]) == (status != "running")
    assert not db.held_run(parent["id"])


class GPUTransport:
    def __init__(self, *_):
        self.busy = False
        self.launches = []

    def call(self, action, **params):
        if action == "probe":
            return {
                "cpu_count": 32,
                "cpu_available": 30,
                "ram_available_mib": 64000,
                "gpu_probe_error": None,
                "received_at": time.time(),
                "gpus": [
                    {
                        "index": i,
                        "uuid": f"GPU-{i}",
                        "name": "fixture",
                        "memory_total_mib": 24000,
                        "memory_free_mib": 24000,
                        "utilization": 0,
                        "mig_enabled": False,
                        "processes": [{"pid": 100}] if self.busy and i == 7 else [],
                    }
                    for i in (0, 1, 3, 7)
                ],
            }
        if action == "evidence":
            return {"files": []}
        if action == "launch":
            self.launches.append(params)
            return {"status": "running"}
        return {"status": "missing"}


class LaunchAgent:
    def __init__(self, *_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def analyze(self, prompt, schema, on_thread, on_turn, **kwargs):
        assert schema is LaunchPlan
        data = json.loads(prompt.split("Evidence JSON:\n", 1)[1])
        assert [gpu["index"] for gpu in data["allocation"]] == [0, 1, 3, 7]
        assert data["logical_gpu_ids"] == [0, 1, 2, 3]
        on_thread("new-launch-task", "codex://threads/new-launch-task")
        on_turn("launch-turn")
        return LaunchPlan(
            command="python train.py --devices 0,1,2,3 --nproc 4",
            rationale="Map allocated UUIDs to logical devices",
        )


async def test_new_launch_agent_uses_actual_gpus_and_rechecks_capacity(db):
    job = db.submit(
        spec(db, command="python train.py --devices 0,1,2,3", resources={"gpu_count": 4})
    )
    transport = GPUTransport()
    scheduler = Scheduler(
        db.home, Settings(), transport_factory=lambda *_: transport, agent_factory=LaunchAgent
    )
    server = Server.model_validate(db.server("local")["config"])
    await scheduler.host_tick(server)
    current = db.job(job["id"])
    assert current["launch_status"] == "pending"
    assert not transport.launches
    agent = db.start_agent(job["id"], "launch", "gpt-5.6-luna")
    await scheduler.analyze(current, "launch", agent)
    assert db.job(job["id"])["launch_status"] == "completed"
    transport.busy = True
    await scheduler.reconcile(server, db.job(job["id"]))
    assert not transport.launches
    assert len(db.leases("local")) == 4
    transport.busy = False
    await scheduler.reconcile(server, db.job(job["id"]))
    payload = transport.launches[0]["spec"]
    assert payload["gpu_uuids"] == ["GPU-0", "GPU-1", "GPU-3", "GPU-7"]
    assert payload["command"].endswith("--devices 0,1,2,3 --nproc 4")
    assert db.last_agent(job["id"], "launch")["thread_id"] == "new-launch-task"


@pytest.mark.parametrize("late_result", ["failure", "success"])
async def test_cancel_during_launch_does_not_execute_or_revive_job(db, late_result):
    job = db.submit(spec(db))
    db.reserve(job["id"], [])
    agent = db.start_agent(job["id"], "launch", "fixture")
    db.cancel(job["id"])
    scheduler = Scheduler(db.home, Settings())
    server = Server.model_validate(db.server("local")["config"])
    await scheduler.reconcile(server, db.job(job["id"]))
    if late_result == "failure":
        db.fail_agent(agent, "late failure", 1)
    else:
        db.complete_agent(agent, LaunchPlan(command="true", rationale="late").model_dump())
    assert db.job(job["id"])["status"] == "cancelled"
    assert not db.active_runs("local")
    assert not (db.home / "worker/runs" / db.job(job["id"])["run_id"] / "spec.json").exists()


def test_launch_failure_releases_resources_and_legacy_review_can_retry(db):
    job = db.submit(spec(db))
    previous = db.reserve(job["id"], [])
    agent = db.start_agent(job["id"], "launch", "fixture")
    db.fail_agent(agent, "ambiguous configuration", 1)
    assert db.job(job["id"])["status"] == "failed"
    assert db.run(previous)["result"]["not_launched"]
    assert not db.active_runs("local")
    with db.connection(write=True) as con:
        con.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (job["id"],))
    db.retry_agent(job["id"], "launch")
    assert db.reserve(job["id"], []) != previous


async def test_cpu_launch_cannot_rewrite_experiment_or_output_paths(db):
    class WrongPlan(LaunchAgent):
        async def analyze(self, *args, **kwargs):
            return LaunchPlan(command='cd "$DEEPQUEUE_RUN_DIR"; true', rationale="wrong cwd")

    job = db.submit(spec(db))
    db.reserve(job["id"], [])
    agent = db.start_agent(job["id"], "launch", "fixture")
    scheduler = Scheduler(db.home, Settings(agent_max_attempts=1), agent_factory=WrongPlan)
    await scheduler.analyze(db.job(job["id"]), "launch", agent)
    assert db.job(job["id"])["status"] == "failed"
    assert db.run(db.job(job["id"])["run_id"])["result"]["not_launched"]


def test_cancelling_finished_parent_stops_its_running_child(db):
    parent = finish_parent(db, max_improvement_rounds=2)
    child = db.submit(spec(db, parent_job_id=parent["id"]))
    db.reserve(child["id"], [])
    db.running(child["id"])
    db.cancel(parent["id"])
    assert db.job(child["id"])["cancel_requested"]
    assert db.job(child["id"])["completion_mode"] == "finish"


async def test_worker_executes_per_run_config_and_keeps_tmux_output(db):
    job = db.submit(spec(db, command="exit 99", source_thread_id=None, archive=False))
    run = db.reserve(job["id"], [])
    code = "import os; print('accuracy=0.875', os.environ['EXPERIMENT_MODE'], flush=True)"
    plan = LaunchPlan.model_validate(
        {
            "command": shlex.quote(sys.executable) + ' "$DEEPQUEUE_RUN_DIR/launch.py"',
            "env": [{"name": "EXPERIMENT_MODE", "value": "adapted"}],
            "files": [{"path": "launch.py", "content": code}],
            "rationale": "CPU diagnostic",
        }
    )
    agent = db.start_agent(job["id"], "launch", "fixture")
    db.complete_agent(agent, plan.model_dump())
    server = Server.model_validate(db.server("local")["config"])
    scheduler = Scheduler(db.home, Settings())
    client = Transport(db.home, server)
    session = "deepqueue-" + run
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await scheduler.reconcile(server, db.job(job["id"]))
            if db.job(job["id"])["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.1)
        assert db.job(job["id"])["status"] == "succeeded"
        terminal = client.terminal(run)
        assert terminal["available"] and session in terminal["attach_command"]
        await asyncio.sleep(1.1)
        capture = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", "-", "-t", session],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "accuracy=0.875 adapted" in capture
        assert "accuracy=0.875 adapted" in client.call("logs", run_id=run)["text"]
        assert (db.home / "worker/runs" / run / "config/launch.py").read_text() == code
        assert not (db.home / "launch.py").exists()
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def test_schema_three_upgrade_keeps_existing_records(db):
    job = db.submit(spec(db))
    before = db.detail(job["id"])
    with db.connection(write=True) as con:
        con.execute("DROP INDEX jobs_parent")
        con.execute("DROP TABLE resource_holds")
        con.execute("ALTER TABLE jobs DROP COLUMN completion_mode")
        con.execute("ALTER TABLE jobs DROP COLUMN launch_status")
        con.execute("ALTER TABLE runs DROP COLUMN launch_plan")
        con.execute("PRAGMA user_version=3")
    Database(db.home).ensure_schema()
    assert db.schema_version() == SCHEMA_VERSION
    assert db.detail(job["id"]) == before
