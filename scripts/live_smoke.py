"""Run two harmless CPU experiments using the real local Codex Luna model."""

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
        raise ValueError("Use a new state directory for the live smoke test")
    settings = initialize(home)
    settings.poll_seconds = 0.5
    settings.agent_max_attempts = 1
    settings.max_agents = 2
    private_write(home / "config.json", settings.model_dump_json(indent=2))
    project = home / "project"
    project.mkdir()
    source = Path(__file__).resolve().parents[1] / "examples" / "cpu_experiment.py"
    shutil.copy2(source, project / "cpu_experiment.py")
    db = Database(home)
    db.initialize()
    db.add_server(Server(name="local", worker_root=str(home / "worker"), max_running=1))
    batch = db.submit_batch(
        {
            "name": "luna-live-smoke",
            "defaults": {
                "server": "local",
                "cwd": str(project),
                "context_files": ["cpu_experiment.py"],
                "intent": "Two tiny CPU-only synthetic experiments for queue verification. "
                "No GPU is needed.",
            },
            "experiments": [
                {
                    "key": "lr-001",
                    "command": "python3 cpu_experiment.py --learning-rate 0.01 "
                    "--output results/slow.json",
                    "artifact_files": ["results/slow.json"],
                },
                {
                    "key": "lr-01",
                    "command": "python3 cpu_experiment.py --learning-rate 0.1 "
                    "--output results/fast.json",
                    "artifact_files": ["results/fast.json"],
                    "depends_on": ["lr-001"],
                },
            ],
        }
    )
    print(json.dumps({"home": str(home), **batch}), flush=True)
    scheduler = Scheduler(home, settings)
    scheduler.acquire()
    deadline = time.monotonic() + 600
    previous = None
    try:
        while time.monotonic() < deadline:
            await scheduler.tick()
            jobs = db.jobs()
            state = [(job["spec"]["title"], job["status"], job["archive_status"]) for job in jobs]
            if state != previous:
                print(json.dumps({"state": state}), flush=True)
                previous = state
            if any(
                job["status"] in ("needs_review", "failed", "lost", "blocked")
                or job["archive_status"] == "failed"
                for job in jobs
            ):
                raise RuntimeError(json.dumps([db.detail(job["id"]) for job in jobs]))
            if all(
                job["status"] == "succeeded" and job["archive_status"] == "completed"
                for job in jobs
            ):
                result = {
                    "model": settings.model,
                    "home": str(home),
                    "jobs": [db.detail(job["id"]) for job in jobs],
                }
                private_write(
                    home / "verification.json", json.dumps(result, ensure_ascii=False, indent=2)
                )
                print(
                    json.dumps(
                        {
                            "verified": True,
                            "report": str(home / "verification.json"),
                            "threads": [
                                {"phase": a["phase"], "model": a["model"], "url": a["deep_link"]}
                                for job in result["jobs"]
                                for a in job["agents"]
                            ],
                        }
                    ),
                    flush=True,
                )
                return
            await asyncio.sleep(settings.poll_seconds)
        raise TimeoutError("Live smoke test exceeded 600 seconds")
    finally:
        for job in db.jobs(["pending", "estimating", "queued", "starting", "running"]):
            db.cancel(job["id"])
        for task in scheduler.agents.values():
            task.cancel()
        await asyncio.gather(*scheduler.agents.values(), return_exceptions=True)
        for job in db.jobs(["starting", "running"]):
            await scheduler.reconcile(Server.model_validate(db.server("local")["config"]), job)
        scheduler.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    asyncio.run(run(parser.parse_args().home.resolve()))
