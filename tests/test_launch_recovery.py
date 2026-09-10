import json
import time

import pytest
from fastapi.testclient import TestClient

from deepqueue.config import update_settings
from deepqueue.db import SCHEMA_VERSION, Database
from deepqueue.models import JobSpec, LaunchPlan, Server, Settings
from deepqueue.scheduler import Scheduler, launch_failure_kind
from deepqueue.web import create_app
from deepqueue.worker import gpu_binding_error


class RecoveryTransport:
    def __init__(self):
        self.launches = {}
        self.results = {}
        self.output = {}
        self.busy = set()

    def call(self, action, **params):
        if action == "probe":
            return {
                "cpu_count": 64,
                "cpu_available": 64,
                "ram_available_mib": 128000,
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
                        "processes": [{"pid": 42}] if i in self.busy else [],
                    }
                    for i in (0, 1, 3, 7, 8)
                ],
            }
        if action == "evidence":
            return {"files": []}
        run_id = params["run_id"]
        if action == "launch":
            assert run_id not in self.launches
            self.launches[run_id] = params["spec"]
            self.results[run_id] = {"status": "running"}
        if action == "logs":
            return {"text": self.output.get(run_id, ""), "bytes": 0, "truncated": False}
        return self.results.get(run_id, {"status": "missing"})


class RecoveryAgent:
    evidence = []

    def __init__(self, settings, directory):
        self.directory = directory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def analyze(self, prompt, schema, on_thread, on_turn, **kwargs):
        assert schema is LaunchPlan
        data = json.loads(prompt.split("Evidence JSON:\n", 1)[1])
        self.evidence.append(data)
        assert "Physical GPUs 0,1,3,7 become" not in prompt
        assert data["resource_review"] == "approved_for_execution"
        assert data["estimate_is_advisory"]
        assert [item["physical_index"] for item in data["gpu_mapping"]] == [
            item["index"] for item in data["allocation"]
        ]
        count = len(data["allocation"])
        batch = 8 // (2 ** (data.get("recovery") or {}).get("attempt", 0))
        on_thread(self.directory.name, "codex://threads/" + self.directory.name)
        on_turn("turn")
        return LaunchPlan(
            command=f"python train.py --devices {','.join(map(str, range(count)))} "
            f"--nproc {count} --batch-size {batch}",
            rationale=f"Use logical devices and batch size {batch}",
            needs_review=True,
        )


async def prepared_job(db, **settings):
    job = db.submit(
        JobSpec(
            server="local",
            cwd=str(db.home),
            command="python train.py --devices 4,5,6,7 --nproc 4 --batch-size 8",
            resources={"gpu_count": 4},
            skip_estimate=True,
            source_thread_id="origin",
        )
    )
    transport = RecoveryTransport()
    scheduler = Scheduler(
        db.home,
        Settings(**settings),
        transport_factory=lambda *_: transport,
        agent_factory=RecoveryAgent,
    )
    server = Server.model_validate(db.server("local")["config"])
    await scheduler.host_tick(server)
    return job, transport, scheduler, server


async def plan_and_launch(db, job, scheduler, server):
    agent = db.start_agent(job["id"], "launch", "gpt-5.6-luna")
    await scheduler.analyze(db.job(job["id"]), "launch", agent)
    assert db.job(job["id"])["launch_status"] == "completed"
    await scheduler.reconcile(server, db.job(job["id"]))
    return db.job(job["id"])["run_id"]


async def test_oom_repairs_keep_reservation_history_and_archive_only_final_result(db):
    job, transport, scheduler, server = await prepared_job(db)
    RecoveryAgent.evidence = []
    first = None
    for attempt in range(3):
        run_id = await plan_and_launch(db, job, scheduler, server)
        first = first or run_id
        assert transport.launches[run_id]["command"].endswith(f"--batch-size {8 // 2**attempt}")
        assert transport.launches[run_id]["gpu_uuids"] == ["GPU-0", "GPU-1", "GPU-3", "GPU-7"]
        if attempt < 2:
            stale = db.job(job["id"])
            transport.results[run_id] = {"status": "failed", "exit_code": 1}
            transport.output[run_id] = "torch.cuda.OutOfMemoryError: CUDA out of memory"
            await scheduler.reconcile(server, stale)
            current = db.job(job["id"])
            assert current["status"] == "starting"
            assert current["launch_retries"] == attempt + 1
            assert current["archive_status"] == "pending"
            assert not db.start_agent(job["id"], "archive", "origin")
            assert {lease["run_id"] for lease in db.leases("local")} == {current["run_id"]}
            await scheduler.reconcile(server, stale)
            assert db.job(job["id"])["run_id"] == current["run_id"]
            assert len(db.detail(job["id"])["runs"]) == attempt + 2
            # A new scheduler can resume the durable pending repair.
            scheduler = Scheduler(
                db.home,
                Settings(),
                transport_factory=lambda *_: transport,
                agent_factory=RecoveryAgent,
            )
        else:
            transport.results[run_id] = {"status": "succeeded", "exit_code": 0}
            await scheduler.reconcile(server, db.job(job["id"]))
    detail = db.detail(job["id"])
    assert detail["status"] == "succeeded"
    assert len(detail["runs"]) == 3
    assert detail["spec"]["command"].endswith("--batch-size 8")
    assert detail["spec"]["source_thread_id"] == "origin"
    assert not db.leases("local")
    assert len(db.jobs()) == 1
    assert RecoveryAgent.evidence[1]["previous_execution"]["id"] == first
    assert "OutOfMemoryError" in RecoveryAgent.evidence[1]["previous_log_tail"]["text"]
    assert all(not run["launch_plan"]["needs_review"] for run in detail["runs"])
    assert [a["run_id"] for a in detail["agents"]] == [r["id"] for r in detail["runs"]]
    archive = await scheduler.evidence(detail, "archive")
    assert len(archive["execution_attempts"]) == 3


async def test_busy_reserved_gpu_can_be_replaced_before_any_execution(db):
    job, transport, scheduler, server = await prepared_job(db)
    agent = db.start_agent(job["id"], "launch", "fixture")
    await scheduler.analyze(db.job(job["id"]), "launch", agent)
    old_id = db.job(job["id"])["run_id"]
    transport.busy = {3}
    await scheduler.reconcile(server, db.job(job["id"]))
    assert not transport.launches
    current = db.job(job["id"])
    assert current["run_id"] != old_id
    assert db.run(old_id)["result"]["not_launched"]
    assert [g["index"] for g in db.run(current["run_id"])["allocation"]] == [0, 1, 7, 8]
    run_id = await plan_and_launch(db, job, scheduler, server)
    assert transport.launches[run_id]["gpu_uuids"] == ["GPU-0", "GPU-1", "GPU-7", "GPU-8"]


async def test_recovery_lease_conflict_rolls_back_the_whole_attempt(db):
    job, _, scheduler, server = await prepared_job(db)
    run_id = await plan_and_launch(db, job, scheduler, server)
    other = db.submit(
        JobSpec(
            server="local",
            cwd=str(db.home),
            command="true",
            resources={"gpu_count": 1},
            skip_estimate=True,
        )
    )
    busy = {"index": 8, "uuid": "GPU-8", "name": "fixture"}
    other_id = db.reserve(other["id"], [busy])
    assert other_id
    leases = db.leases("local")
    run = db.run(run_id)
    assert (
        db.retry_launch(
            job["id"],
            run_id,
            {"status": "failed", "exit_code": 1},
            "gpu_mapping",
            3,
            allocation=[*run["allocation"][:3], busy],
        )
        is None
    )
    assert db.leases("local") == leases
    assert db.run(run_id) == run
    assert db.job(job["id"])["launch_retries"] == 0
    assert len(db.detail(job["id"])["runs"]) == 1


async def test_new_launch_agent_receives_previous_validation_failure(db):
    job, _, scheduler, _ = await prepared_job(db)
    agent = db.start_agent(job["id"], "launch", "fixture")
    db.fail_agent(agent, "Executable contains literal backslashes", 3)
    assert db.start_agent(job["id"], "launch", "fixture")
    evidence = await scheduler.evidence(db.job(job["id"]), "launch")
    assert evidence["previous_plan_error"] == "Executable contains literal backslashes"


@pytest.mark.parametrize("case", ["limit", "disabled", "cancel", "unrelated", "lost", "mapping"])
async def test_automatic_repair_is_bounded_and_respects_cancellation(db, case):
    job, transport, scheduler, server = await prepared_job(
        db, launch_max_retries=0 if case == "disabled" else 1
    )
    run_id = await plan_and_launch(db, job, scheduler, server)
    transport.results[run_id] = {"status": "lost" if case == "lost" else "failed", "exit_code": 1}
    transport.output[run_id] = (
        "CUDA error: invalid device ordinal"
        if case == "mapping"
        else "ValueError: dataset not found"
        if case == "unrelated"
        else "CUDA out of memory"
    )
    if case == "cancel":
        db.cancel(job["id"])
        transport.results[run_id] = {"status": "cancelled"}
    await scheduler.reconcile(server, db.job(job["id"]))
    if case in ("limit", "mapping"):
        assert db.job(job["id"])["launch_retries"] == 1
        run_id = await plan_and_launch(db, job, scheduler, server)
        transport.results[run_id] = {"status": "failed", "exit_code": 1}
        transport.output[run_id] = "CUDA out of memory"
        await scheduler.reconcile(server, db.job(job["id"]))
    detail = db.detail(job["id"])
    assert detail["status"] == {"cancel": "cancelled", "lost": "lost"}.get(case, "failed")
    assert bool(db.leases("local")) == (case == "lost")
    assert detail["launch_retries"] <= 1
    if case == "limit":
        assert "上限" in detail["reason"]


async def test_oom_plan_must_change_execution_and_failed_planning_finishes_automatically(db):
    job, transport, scheduler, server = await prepared_job(db, agent_max_attempts=1)
    run_id = await plan_and_launch(db, job, scheduler, server)
    transport.results[run_id] = {"status": "failed", "exit_code": 1}
    transport.output[run_id] = "CUDA out of memory"
    await scheduler.reconcile(server, db.job(job["id"]))

    class NoChange(RecoveryAgent):
        async def analyze(self, *args, **kwargs):
            return LaunchPlan(command=transport.launches[run_id]["command"], rationale="No change")

    scheduler.agent_factory = NoChange
    agent = db.start_agent(job["id"], "launch", "fixture")
    await scheduler.analyze(db.job(job["id"]), "launch", agent)
    assert db.job(job["id"])["status"] == "failed"
    assert not db.leases("local")
    assert db.job(job["id"])["archive_status"] == "pending"


@pytest.mark.parametrize(
    "log,kind",
    [
        ("torch.OutOfMemoryError: allocation failed", "oom"),
        ("RuntimeError: CUDA error: out of memory", "oom"),
        ("CUDNN_STATUS_ALLOC_FAILED", "oom"),
        ("CUDA error: invalid device ordinal", "gpu_mapping"),
        ("NCCL: Duplicate GPU detected", "gpu_mapping"),
        ("NCCL connection timed out", None),
        ("Killed", None),
        ("Warning: reduce batch size if OOM occurs", None),
    ],
)
def test_only_observed_memory_and_mapping_failures_trigger_recovery(log, kind):
    assert launch_failure_kind({"status": "failed"}, log) == kind
    assert launch_failure_kind({"status": "succeeded"}, log) is None
    assert launch_failure_kind({"status": "failed", "error": "Runtime limit exceeded"}, log) is None


def test_gpu_binding_audit_uses_only_this_process_group_and_allows_warmup():
    allocated = ["GPU-0", "GPU-3"]
    assert gpu_binding_error(allocated, {"GPU-7": 100}, {"GPU-7"}, 0)
    assert gpu_binding_error(allocated, {"GPU-0": 100}, {"GPU-0"}, 119) is None
    assert gpu_binding_error(allocated, {"GPU-0": 100}, {"GPU-0"}, 120)
    assert gpu_binding_error(allocated, {}, set(allocated), 300) is None


@pytest.mark.parametrize("executable", ["' /venv/bin/python'", r"'\/venv\/bin\/python'"])
def test_generated_executable_path_cannot_gain_whitespace_or_json_escapes(executable):
    with pytest.raises(ValueError, match="Executable contains"):
        LaunchPlan(command=executable + " train.py --batch-size 2", rationale="repair")
    plan = LaunchPlan(
        command="'/venv/space dir/python' train.py --batch-size 2", rationale="repair"
    )
    assert plan.command.startswith("'/venv/space dir/")


@pytest.mark.parametrize(
    "executable", ["' /venv/space dir/python'", r"'\/venv\/space dir\/python'"]
)
@pytest.mark.parametrize("separator", [" ", "  ", "\n"])
def test_known_executable_repair_preserves_shell_tail_verbatim(executable, separator):
    tail = 'train.py --batch-size 2 > "$OUTPUT_FILE" && printf done'
    original = "'/venv/space dir/python' train.py --batch-size 8"
    result = LaunchPlan.model_validate(
        {"command": executable + separator + tail, "rationale": "repair"},
        context={"reference_command": original},
    )
    assert result.command == "'/venv/space dir/python'" + separator + tail
    assert "解释器路径" in result.rationale
    with pytest.raises(ValueError, match="Executable contains"):
        LaunchPlan.model_validate(
            {"command": executable + " " + tail, "rationale": "repair"},
            context={"reference_command": "/another/python train.py"},
        )


async def test_history_logs_are_scoped_and_settings_survive_old_clients(db, monkeypatch):
    job, transport, scheduler, server = await prepared_job(db)
    run_id = await plan_and_launch(db, job, scheduler, server)
    other = db.submit(JobSpec(server="local", cwd=str(db.home), command="true"))
    monkeypatch.setattr("deepqueue.web.Transport", lambda *_: transport)
    with TestClient(
        create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"}
    ) as web:
        assert web.get(f"/api/jobs/{job['id']}/logs", params={"run_id": run_id}).status_code == 200
        assert (
            web.get(f"/api/jobs/{other['id']}/logs", params={"run_id": run_id}).status_code == 404
        )
        assert (
            web.get(f"/api/jobs/{other['id']}/terminal", params={"run_id": run_id}).status_code
            == 404
        )
        settings = {"agent_models": {}, "agent_efforts": {}, "launch_max_retries": 5}
        assert web.post("/api/settings", json=settings).json()["launch_max_retries"] == 5
        settings.pop("launch_max_retries")
        assert web.post("/api/settings", json=settings).json()["launch_max_retries"] == 5
        settings["launch_max_retries"] = 11
        assert web.post("/api/settings", json=settings).status_code == 422
    update_settings(db.home, {"launch_max_retries": 2})
    scheduler.reload_agent_settings()
    assert scheduler.settings.launch_max_retries == 2


def test_schema_five_upgrade_preserves_jobs_and_defaults(db):
    job = db.submit(JobSpec(server="local", cwd=str(db.home), command="true"))
    with db.connection(write=True) as con:
        con.execute("ALTER TABLE jobs DROP COLUMN launch_retries")
        con.execute("ALTER TABLE runs DROP COLUMN recovery")
        con.execute("ALTER TABLE agent_runs DROP COLUMN run_id")
        con.execute("PRAGMA user_version=5")
    Database(db.home).ensure_schema()
    assert db.schema_version() == SCHEMA_VERSION
    assert db.job(job["id"])["launch_retries"] == 0
    assert db.job(job["id"])["spec"]["command"] == "true"
