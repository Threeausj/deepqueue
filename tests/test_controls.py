import asyncio
import concurrent.futures
import json

import pytest

from deepqueue.db import SCHEMA_VERSION, Database
from deepqueue.models import JobSpec, Resources, Server, Settings
from deepqueue.scheduler import Scheduler


def batch_manifest(home):
    return {
        "name": "controls",
        "defaults": {
            "server": "local",
            "cwd": str(home),
            "command": "true",
            "resources": {"ram_mib": 128},
            "skip_estimate": True,
        },
        "experiments": [{"key": key} for key in ("first", "second", "third")],
    }


def test_pause_scopes_survive_restart_without_resuming_individual_jobs(db):
    manifest = batch_manifest(db.home)
    ids = db.submit_batch(manifest)["jobs"]
    independent = db.submit(JobSpec.model_validate({**manifest["defaults"], "group": "controls"}))
    db.set_paused(ids["first"], True)
    db.set_batch_paused("controls", True)
    restarted = Database(db.home)
    restarted.ensure_schema()
    assert restarted.job(ids["first"])["pause_sources"] == ["job", "batch"]
    assert not restarted.job(independent["id"])["pause_sources"]
    assert restarted.reserve(ids["second"], []) is None
    restarted.set_batch_paused("controls", False)
    assert restarted.reserve(ids["first"], []) is None
    assert restarted.reserve(ids["second"], [])
    assert restarted.submit_batch(manifest)["existing"]
    restarted.set_paused(ids["first"], False)
    assert restarted.reserve(ids["first"], [])


def test_pause_cannot_interrupt_admitted_run_or_its_archive(db):
    ids = db.submit_batch(batch_manifest(db.home))["jobs"]
    run = db.reserve(ids["first"], [])
    db.set_batch_paused("controls", True)
    with pytest.raises(ValueError, match="not started"):
        db.set_paused(ids["first"], True)
    db.running(ids["first"])
    assert db.job(ids["first"])["run_id"] == run
    db.finish(ids["first"], {"status": "succeeded", "exit_code": 0})
    assert ids["first"] in [job["id"] for job in db.agent_candidates()]
    assert db.start_agent(ids["first"], "archive", "test-fixture")
    assert db.reserve(ids["second"], []) is None


def test_pause_gates_estimates_transactionally_but_preserves_finished_estimates(db):
    spec = JobSpec(server="local", cwd=str(db.home), command="true")
    first, second = db.submit(spec), db.submit(spec)
    candidates = db.agent_candidates()
    assert len(candidates) == 2
    started = db.start_agent(first["id"], "estimate", "test-fixture")
    db.set_paused(first["id"], True)
    db.set_paused(second["id"], True)
    assert not db.agent_candidates()
    assert db.start_agent(second["id"], "estimate", "test-fixture") is None
    db.complete_agent(started, {"rationale": "CPU command"}, "queued", Resources().model_dump())
    assert db.job(first["id"])["status"] == "queued"
    assert db.reserve(first["id"], []) is None


async def test_stale_server_snapshot_cannot_dispatch_after_pause(db):
    job = db.submit(JobSpec.model_validate(batch_manifest(db.home)["defaults"]))
    stale_server = Server.model_validate(db.server("local")["config"])
    db.enable_server("local", False)

    class Probe:
        def __init__(self, home, server):
            pass

        def call(self, action, **params):
            assert action == "probe"
            return {"cpu_count": 16, "cpu_available": 16, "ram_available_mib": 64000, "gpus": []}

    await Scheduler(db.home, Settings(), transport_factory=Probe).host_tick(stale_server)
    assert db.job(job["id"])["status"] == "queued"
    assert not db.active_runs("local")


def test_priority_changes_order_without_changing_submission_identity(db):
    manifest = batch_manifest(db.home)
    ids = db.submit_batch(manifest)["jobs"]
    db.reserve(ids["first"], [])
    assert db.set_batch_priority("controls", 20) == {"updated": 2, "eligible": 2, "total": 3}
    db.set_priority(ids["third"], 50)
    ordered = Scheduler(db.home, Settings()).queue_order(db.jobs(["queued"]))
    assert [job["id"] for job in ordered] == [ids["third"], ids["second"]]
    assert db.job(ids["first"])["priority"] == 0
    assert db.job(ids["third"])["spec"]["priority"] == 0
    assert db.submit_batch(manifest)["existing"]
    assert db.preview_batch(manifest)["existing"]
    with pytest.raises(ValueError, match="not started"):
        db.set_priority(ids["first"], 30)
    with pytest.raises(ValueError, match="between"):
        db.set_batch_priority("controls", 101)


def test_batch_cancel_rolls_back_on_failure_and_preserves_running_leases(db, monkeypatch):
    ids = db.submit_batch(batch_manifest(db.home))["jobs"]
    db.reserve(ids["first"], [{"uuid": "GPU-test", "index": 0, "name": "fixture"}])
    original = db._cancel

    def fail_second(con, job):
        original(con, job)
        if job["id"] == ids["second"]:
            raise RuntimeError("interrupted transaction")

    monkeypatch.setattr(db, "_cancel", fail_second)
    with pytest.raises(RuntimeError):
        db.cancel_batch("controls")
    assert not any(job["cancel_requested"] for job in db.jobs())
    monkeypatch.setattr(db, "_cancel", original)
    db.cancel_batch("controls")
    assert db.job(ids["first"])["status"] == "starting"
    assert len(db.leases("local")) == 1
    assert db.job(ids["second"])["status"] == "cancelled"


def downgrade_to_v1(db):
    with db.connection(write=True) as con:
        con.execute("DROP INDEX jobs_parent")
        con.execute("DROP TABLE resource_holds")
        con.execute("ALTER TABLE jobs DROP COLUMN completion_mode")
        con.execute("ALTER TABLE jobs DROP COLUMN launch_status")
        con.execute("ALTER TABLE runs DROP COLUMN launch_plan")
        con.execute("DROP INDEX jobs_batch")
        con.execute("ALTER TABLE jobs DROP COLUMN batch_name")
        con.execute("ALTER TABLE jobs DROP COLUMN paused")
        con.execute("ALTER TABLE batches DROP COLUMN paused")
        con.execute("ALTER TABLE agent_runs DROP COLUMN delivery_id")
        con.execute("ALTER TABLE agent_runs DROP COLUMN request_sent")
        con.execute("PRAGMA user_version=1")


def test_v1_migration_preserves_runs_links_and_authoritative_membership(db):
    manifest = batch_manifest(db.home)
    ids = db.submit_batch(manifest)["jobs"]
    standalone = db.submit(JobSpec.model_validate({**manifest["defaults"], "group": "controls"}))
    db.reserve(ids["first"], [{"uuid": "GPU-v1", "index": 0, "name": "fixture"}])
    db.cancel(ids["second"])
    agent = db.start_agent(ids["second"], "archive", "test-fixture")
    db.agent_link(agent, "thread-v1", "codex://threads/thread-v1")
    before = db.detail(ids["second"])
    downgrade_to_v1(db)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Database(db.home).initialize(), range(2)))
    assert db.schema_version() == SCHEMA_VERSION
    assert db.job(standalone["id"])["batch_name"] is None
    assert db.job(ids["first"])["batch_name"] == "controls"
    assert db.leases("local")[0]["uuid"] == "GPU-v1"
    after = db.detail(ids["second"])
    for key in ("spec", "spec_hash", "status", "agents", "events", "idempotency_key"):
        assert before[key] == after[key]
    assert db.submit_batch(manifest)["existing"]


def test_invalid_v1_membership_rolls_back_entire_migration(db):
    db.submit_batch(batch_manifest(db.home))
    downgrade_to_v1(db)
    with db.connection(write=True) as con:
        con.execute("UPDATE batches SET job_ids=?", (json.dumps({"missing": "absent"}),))
    with pytest.raises(ValueError, match="membership"):
        db.initialize()
    assert db.schema_version() == 1
    with db.connection() as con:
        assert "paused" not in [row["name"] for row in con.execute("PRAGMA table_info(jobs)")]


async def test_estimate_admission_respects_priority_and_skips_paused_jobs(db):
    spec = JobSpec(server="local", cwd=str(db.home), command="true")
    low, held, high = db.submit(spec), db.submit(spec), db.submit(spec)
    db.set_priority(held["id"], 100)
    db.set_paused(held["id"], True)
    db.set_priority(high["id"], 50)
    admitted = []

    class Estimator(Scheduler):
        async def analyze(self, job, phase, agent_id, settings=None):
            admitted.append(job["id"])
            self.db.complete_agent(
                agent_id, {"rationale": "test fixture"}, "queued", Resources().model_dump()
            )

    scheduler = Estimator(db.home, Settings(max_agents=1))
    scheduler.start_agents()
    await asyncio.gather(*scheduler.agents.values())
    scheduler.start_agents()
    await asyncio.gather(*scheduler.agents.values())
    assert admitted == [high["id"], low["id"]]
