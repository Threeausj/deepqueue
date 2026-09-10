import concurrent.futures
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid

import pytest

from deepqueue import worker
from deepqueue.models import Server
from deepqueue.transport import Transport


def rank_command(outcome):
    rank = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    code = (
        "import json,subprocess,sys,time; from pathlib import Path; "
        f"ranks=[subprocess.Popen([sys.executable,'-c',{rank!r}], start_new_session=True) "
        "for _ in range(2)]; "
        "Path('ranks.json').write_text(json.dumps([p.pid for p in ranks])); "
        + (
            "time.sleep(60)"
            if outcome in ("cancel", "timeout", "mapping")
            else f"sys.exit({outcome})"
        )
    )
    return shlex.join([sys.executable, "-c", code])


def transport(tmp_path):
    return Transport(
        tmp_path, Server(name="local", worker_root=str(tmp_path / "worker"), python=sys.executable)
    )


def launch_spec(tmp_path, command, timeout=0):
    return {
        "cwd": str(tmp_path),
        "command": command,
        "env": {},
        "gpu_uuids": [],
        "timeout_seconds": timeout,
    }


def wait_result(client, run_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.call("inspect", run_id=run_id)
        if result["status"] in ("succeeded", "failed", "cancelled", "lost"):
            return result
        time.sleep(0.1)
    raise AssertionError("Worker did not finish before deadline")


def test_concurrent_launch_is_idempotent_and_persists_output(tmp_path):
    client = transport(tmp_path)
    run_id = uuid.uuid4().hex
    spec = launch_spec(
        tmp_path, "printf 'once\n' >> count.txt; printf '%s' \"$CUDA_VISIBLE_DEVICES\""
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(lambda _: client.call("launch", run_id=run_id, spec=spec), range(3))
        )
    assert all(item["status"] in ("starting", "running", "succeeded") for item in results)
    final = wait_result(client, run_id)
    assert final["status"] == "succeeded"
    assert (tmp_path / "count.txt").read_text() == "once\n"
    assert final["gpu_uuids"] == []
    assert client.call("logs", run_id=run_id)["text"] == ""
    assert client.call("launch", run_id=run_id, spec=spec)["status"] == "succeeded"


def test_cancellation_and_timeout_reap_process_group(tmp_path):
    client = transport(tmp_path)
    for cancel in (True, False):
        run_id = uuid.uuid4().hex
        spec = launch_spec(tmp_path, "sleep 60 & wait", timeout=0 if cancel else 1)
        client.call("launch", run_id=run_id, spec=spec)
        time.sleep(0.3)
        if cancel:
            client.call("cancel", run_id=run_id)
        final = wait_result(client, run_id)
        assert final["status"] == ("cancelled" if cancel else "failed")
        if not cancel:
            assert final["error"] == "Runtime limit exceeded"


def test_evidence_is_bounded_and_cannot_escape_cwd(tmp_path):
    client = transport(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "secret.txt").write_text("not evidence")
    (project / "link.txt").symlink_to(tmp_path / "secret.txt")
    (project / "large.txt").write_text("a" * 40000)
    result = client.call(
        "evidence", cwd=str(project), paths=["../secret.txt", "link.txt", "large.txt"]
    )
    assert all("error" in item for item in result["files"][:2])
    assert result["files"][2]["truncated"]
    assert len(result["files"][2]["content"]) == 32768


def test_log_tail_and_failure_exit_code(tmp_path):
    client = transport(tmp_path)
    run_id = uuid.uuid4().hex
    code = "import sys; print('x' * 10000); sys.exit(7)"
    command = shlex.join([sys.executable, "-c", code])
    client.call("launch", run_id=run_id, spec=launch_spec(tmp_path, command))
    result = wait_result(client, run_id)
    assert result["status"] == "failed" and result["exit_code"] == 7
    tail = client.call("logs", run_id=run_id, limit=100)
    assert tail["truncated"] and len(tail["text"]) == 100


@pytest.mark.parametrize("wrong_binding", [True, False])
def test_gpu_binding_failure_reaps_only_its_own_process_group(tmp_path, monkeypatch, wrong_binding):
    directory = tmp_path / "run"
    directory.mkdir()
    spec = launch_spec(tmp_path, "sleep 30 & wait" if wrong_binding else "sleep 0.3")
    spec["gpu_uuids"] = ["GPU-allocated"]
    worker.atomic_json(directory / "spec.json", spec)
    unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)

    def telemetry():
        child = worker.read_json(directory / "state.json")["child"]
        return [
            {"pid": unrelated.pid, "uuid": "GPU-unrelated", "memory_mib": 100},
            {
                "pid": child["pid"],
                "uuid": "GPU-wrong" if wrong_binding else "GPU-allocated",
                "memory_mib": 100,
            },
        ]

    monkeypatch.setattr(worker, "gpu_processes", telemetry)
    try:
        worker.supervise(directory)
        result = worker.read_json(directory / "result.json")
        state = worker.read_json(directory / "state.json")
        assert result["status"] == ("failed" if wrong_binding else "succeeded")
        assert result["failure_kind"] == ("gpu_mapping" if wrong_binding else None)
        assert "GPU-unrelated" not in result["metrics"]["observed_gpu_uuids"]
        assert not worker.group_members(state["child"]["pid"])
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("outcome", [0, 7, "cancel", "timeout"])
def test_worker_reaps_elastic_rank_sessions_even_after_launcher_exits(tmp_path, outcome):
    client = transport(tmp_path)
    run_id = uuid.uuid4().hex
    spec = launch_spec(tmp_path, rank_command(outcome), timeout=1 if outcome == "timeout" else 10)
    client.call("launch", run_id=run_id, spec=spec)
    deadline = time.monotonic() + 5
    while not (tmp_path / "ranks.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    ranks = json.loads((tmp_path / "ranks.json").read_text())
    if outcome == "cancel":
        client.call("cancel", run_id=run_id)
    final = wait_result(client, run_id)
    assert final["status"] == (
        "succeeded" if outcome == 0 else "cancelled" if outcome == "cancel" else "failed"
    )
    assert all(worker.identity(pid) is None for pid in ranks)
    assert all(not worker.group_members(pid) for pid in ranks)


def test_binding_audit_follows_rank_sessions_and_leaves_other_jobs_alive(tmp_path, monkeypatch):
    directory = tmp_path / "run"
    directory.mkdir()
    spec = launch_spec(tmp_path, rank_command("mapping"), timeout=10)
    spec["gpu_uuids"] = ["GPU-allocated"]
    worker.atomic_json(directory / "spec.json", spec)
    previous = worker.subreaper()
    unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
    unrelated_identity = worker.identity(unrelated.pid)

    def telemetry():
        rows = [{"pid": unrelated.pid, "uuid": "GPU-unrelated", "memory_mib": 100}]
        if (tmp_path / "ranks.json").exists():
            for pid in json.loads((tmp_path / "ranks.json").read_text()):
                rows.append({"pid": pid, "uuid": "GPU-wrong", "memory_mib": 100})
        return rows

    monkeypatch.setattr(worker, "gpu_processes", telemetry)
    try:
        worker.supervise(directory)
        result = worker.read_json(directory / "result.json")
        assert result["status"] == "failed"
        assert result["failure_kind"] == "gpu_mapping"
        assert result["metrics"]["observed_gpu_uuids"] == ["GPU-wrong"]
        assert worker.subreaper() == previous
        assert worker.alive(unrelated_identity)
        assert all(
            worker.identity(pid) is None
            for pid in json.loads((tmp_path / "ranks.json").read_text())
        )
    finally:
        if worker.alive(unrelated_identity):
            os.kill(unrelated.pid, signal.SIGTERM)
        unrelated.wait(timeout=5)


def test_inaccessible_nvidia_pids_are_reported_without_invented_gpu_usage(tmp_path, monkeypatch):
    directory = tmp_path / "run"
    directory.mkdir()
    spec = launch_spec(tmp_path, "sleep 0.3")
    spec["gpu_uuids"] = ["GPU-allocated"]
    worker.atomic_json(directory / "spec.json", spec)
    monkeypatch.setattr(
        worker,
        "gpu_processes",
        lambda: [{"pid": 999999999, "uuid": "GPU-allocated", "memory_mib": 100}],
    )
    worker.supervise(directory)
    result = worker.read_json(directory / "result.json")
    assert result["status"] == "succeeded"
    assert "NVIDIA PIDs" in result["metrics"]["gpu_attribution_error"]
    assert not result["metrics"]["observed_gpu_uuids"]
    assert result["failure_kind"] is None
