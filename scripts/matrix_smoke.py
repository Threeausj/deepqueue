"""Verify matrix admission and execution with six CPU-only jobs and no model requests."""

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import Server
from deepqueue.scheduler import Scheduler


async def run(home):
    if home.exists():
        raise ValueError("Use a new local state directory for this smoke test")
    settings = initialize(home)
    project = home / "project"
    project.mkdir()
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "examples/cpu_experiment.py",
        project / "cpu_experiment.py",
    )
    db = Database(home)
    db.initialize()
    server = Server(name="local", worker_root=str(home / "worker"), max_running=2)
    db.add_server(server)
    manifest = {
        "name": "matrix-smoke",
        "defaults": {
            "server": "local",
            "cwd": str(project),
            "resources": {"ram_mib": 128},
            "skip_estimate": True,
            "archive": False,
        },
        "matrix": {"lr": [0.01, 0.1, 0.2], "repeat": [1, 2]},
        "experiments": [
            {
                "key": "trial-{index}",
                "argv": [
                    "python3",
                    "cpu_experiment.py",
                    "--learning-rate",
                    "{lr}",
                    "--output",
                    "results/{key}.json",
                ],
                "artifact_files": ["results/{key}.json"],
            }
        ],
    }
    preview = db.preview_batch(manifest)
    assert preview["experiment_count"] == 6 and not db.jobs()
    submitted = db.submit_batch(manifest)
    assert db.submit_batch(manifest)["existing"]
    print(
        json.dumps(
            {
                "previewed": 6,
                "submitted": len(submitted["jobs"]),
                "agent_calls": 0,
                "home": str(home),
            }
        ),
        flush=True,
    )
    scheduler = Scheduler(home, settings)
    scheduler.acquire()
    deadline = time.monotonic() + 90
    last_state = None
    try:
        while time.monotonic() < deadline:
            await scheduler.tick()
            jobs = db.jobs()
            state = [job["status"] for job in jobs]
            if state != last_state:
                print(json.dumps({"states": state}), flush=True)
                last_state = state
            if any(status in ("failed", "lost") for status in state):
                raise RuntimeError("Matrix smoke job failed; inspect the isolated queue")
            if all(status == "succeeded" for status in state):
                events, rows = [], []
                for job in jobs:
                    detail = db.detail(job["id"])
                    assert len(detail["runs"]) == 1 and not detail["agents"]
                    result = detail["runs"][0]["result"]
                    events.extend([(result["started_at"], 1), (result["finished_at"], -1)])
                    metrics_path = project / job["spec"]["artifact_files"][0]
                    metrics = json.loads(metrics_path.read_text())
                    assert metrics["learning_rate"] == job["spec"]["parameters"]["lr"]
                    rows.append(
                        {
                            "id": job["id"],
                            "key": job["spec"]["title"],
                            "parameters": job["spec"]["parameters"],
                            "metrics": metrics,
                        }
                    )
                concurrent = peak = 0
                for _, delta in sorted(events):
                    concurrent += delta
                    peak = max(peak, concurrent)
                assert peak <= server.max_running
                report = {"verified": True, "jobs": rows, "peak_concurrent": peak, "agent_calls": 0}
                private_write(home / "verification.json", json.dumps(report, indent=2))
                print(
                    json.dumps(
                        {
                            "verified": True,
                            "peak_concurrent": peak,
                            "report": str(home / "verification.json"),
                        }
                    ),
                    flush=True,
                )
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("Matrix smoke test exceeded 90 seconds")
    finally:
        for job in db.jobs(["queued", "starting", "running"]):
            db.cancel(job["id"])
        for job in db.jobs(["starting", "running"]):
            await scheduler.reconcile(server, db.job(job["id"]))
        scheduler.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    asyncio.run(run(parser.parse_args().home.resolve()))
