import concurrent.futures
import time

import pytest
from pydantic import ValidationError

from deepqueue.models import Estimate, JobSpec, Resources, Server
from deepqueue.scheduler import Scheduler, fit


def job_spec(**kwargs):
    return JobSpec(
        server="local",
        cwd="/tmp",
        command="true",
        resources=Resources(),
        skip_estimate=True,
        archive=False,
        **kwargs,
    )


def test_idempotent_submission_rejects_changed_command(db):
    spec = job_spec(idempotency_key="one")
    job = db.submit(spec)
    assert db.submit(spec)["id"] == job["id"]
    with pytest.raises(ValueError, match="different job"):
        db.submit(spec.model_copy(update={"command": "false"}))
    assert len(db.jobs()) == 1


def test_batch_atomicity_dependencies_and_idempotency(db):
    manifest = {
        "name": "round",
        "defaults": job_spec().model_dump(),
        "experiments": [
            {"key": "evaluate", "command": "true", "depends_on": ["train"]},
            {"key": "train", "command": "true"},
        ],
    }
    result = db.submit_batch(manifest)
    assert db.submit_batch(manifest)["existing"]
    assert db.job(result["jobs"]["evaluate"])["spec"]["depends_on"] == [result["jobs"]["train"]]
    assert len(db.jobs()) == 2
    invalid = {
        **manifest,
        "name": "invalid",
        "experiments": [
            {"key": "ok", "command": "true"},
            {"key": "bad", "server": "absent", "command": "true"},
        ],
    }
    with pytest.raises(ValueError, match="Unknown server"):
        db.submit_batch(invalid)
    assert len(db.jobs()) == 2
    assert len(db.batches()) == 1


def test_batch_rejects_cycles_without_partial_jobs(db):
    with pytest.raises(ValueError, match="cycle"):
        db.submit_batch(
            {
                "name": "cyclic",
                "defaults": job_spec().model_dump(),
                "experiments": [
                    {"key": "a", "depends_on": ["b"]},
                    {"key": "b", "depends_on": ["a"]},
                ],
            }
        )
    assert not db.jobs()


def test_gpu_lease_concurrent_reservation_and_lost_retention(db):
    jobs = [db.submit(job_spec()) for _ in range(2)]
    allocation = [{"index": 0, "uuid": "GPU-0", "name": "test"}]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda job: db.reserve(job["id"], allocation), jobs))
    assert sum(run is not None for run in runs) == 1
    job = next(job for job, run in zip(jobs, runs, strict=True) if run)
    db.finish(job["id"], {"status": "lost", "error": "disconnected"})
    assert len(db.leases("local")) == 1
    assert len(db.active_runs("local")) == 1
    db.finish(job["id"], {"status": "failed", "note": "verified stopped"}, resolve=True)
    assert not db.leases("local")
    assert not db.active_runs("local")


def test_cancel_during_estimation_does_not_resurrect_job(db):
    spec = job_spec().model_copy(update={"skip_estimate": False})
    job = db.submit(spec)
    agent = db.start_agent(job["id"], "estimate", "gpt-5.6-luna")
    db.cancel(job["id"])
    result = Estimate(
        resources=Resources(),
        confidence=0.9,
        rationale="CPU",
        evidence=[],
        warnings=[],
        needs_review=False,
    )
    db.complete_agent(agent, result.model_dump(), "queued", result.resources.model_dump())
    assert db.job(job["id"])["status"] == "cancelled"


def test_model_validation_preserves_gpu_contract():
    with pytest.raises(ValidationError):
        Resources(gpu_count=2, gpu_ids=["0"])
    with pytest.raises(ValidationError):
        JobSpec(server="local", cwd="/tmp", command="true", skip_estimate=True)
    with pytest.raises(ValidationError, match="GPU selection"):
        JobSpec(server="local", cwd="/tmp", command="CUDA_VISIBLE_DEVICES=0 python train.py")
    with pytest.raises(ValidationError):
        Resources(cpu_cores=-1)


def snapshot():
    return {
        "received_at": time.time(),
        "cpu_count": 16,
        "cpu_available": 12,
        "ram_available_mib": 64000,
        "gpu_probe_error": None,
        "gpus": [
            {
                "index": i,
                "uuid": f"GPU-{i}",
                "name": "4090",
                "memory_total_mib": 24000,
                "memory_free_mib": 23900,
                "utilization": 0,
                "processes": [],
                "mig_enabled": False,
            }
            for i in range(4)
        ],
    }


def test_fit_respects_external_processes_leases_and_stale_telemetry():
    data = snapshot()
    data["gpus"][0]["processes"] = [{"pid": 123}]
    data["gpus"][1]["utilization"] = 99
    resources = Resources(gpu_count=1, gpu_memory_mib=20000)
    allocation, _ = fit(resources, Server(name="local"), data, [], [{"uuid": "GPU-2"}])
    assert [gpu["index"] for gpu in allocation] == [3]
    allocation, reason = fit(
        resources, Server(name="local"), data, [], [{"uuid": "GPU-2"}, {"uuid": "GPU-3"}]
    )
    assert allocation is None and "0 available" in reason
    data["received_at"] -= 100
    assert fit(Resources(), Server(name="local"), data, [], [])[0] is None


def test_fit_does_not_overcommit_ram_or_cpu():
    data = snapshot()
    active = [{"resources": Resources(cpu_cores=10, ram_mib=62000).model_dump()}]
    assert fit(Resources(cpu_cores=3), Server(name="local"), data, active, [])[0] is None
    assert fit(Resources(ram_mib=2000), Server(name="local"), data, active, [])[0] is None


def test_pinned_gpu_order_and_server_allowlist():
    data = snapshot()
    resources = Resources(gpu_count=2, gpu_ids=["3", "1"])
    allocated, _ = fit(resources, Server(name="local"), data, [], [])
    assert [gpu["index"] for gpu in allocated] == [3, 1]
    assert fit(resources, Server(name="local", gpu_allowlist=["1"]), data, [], [])[0] is None


async def test_scheduler_dependencies_and_single_owner(db):
    from deepqueue.config import load_settings

    class FakeTransport:
        def __init__(self, *_):
            pass

        def call(self, action, **_):
            if action == "probe":
                return snapshot()
            if action == "inspect":
                return {"status": "missing"}
            return {"status": "running"}

    first = db.submit(job_spec(launch_agent=False, tmux=False))
    second = db.submit(job_spec(depends_on=[first["id"]], launch_agent=False, tmux=False))
    scheduler = Scheduler(db.home, load_settings(db.home), transport_factory=FakeTransport)
    scheduler.acquire()
    other = Scheduler(db.home, load_settings(db.home))
    try:
        with pytest.raises(ValueError, match="already owns"):
            other.acquire()
        await scheduler.tick()
        assert db.job(first["id"])["status"] == "running"
        assert db.job(second["id"])["status"] == "queued"
        db.finish(first["id"], {"status": "failed", "exit_code": 1})
        await scheduler.tick()
        assert db.job(second["id"])["status"] == "blocked"
    finally:
        scheduler.release()
