"""Real Luna launch planning against synthetic, non-contiguous GPU telemetry; no training."""

import argparse
import asyncio
import json
import shlex
import time
from pathlib import Path

from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import JobSpec, Resources, Server
from deepqueue.scheduler import Scheduler
from deepqueue.transport import Transport


async def run(home):
    if home.exists():
        raise ValueError("Choose a new local directory")
    settings = initialize(home)
    settings.agent_socket = "auto"
    settings.agent_max_attempts = 1
    db = Database(home)
    db.initialize()
    server = Server(name="local", worker_root=str(home / "worker"))
    db.add_server(server)
    private_write(
        home / "train.py",
        """import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--devices", help="Comma-separated logical CUDA indexes after visibility")
parser.add_argument("--nproc", type=int, help="One distributed process per visible GPU")
parser.add_argument("--output")
args = parser.parse_args()
# Interface-only fixture. No GPU training is performed by this test.
""",
    )
    transport = Transport(home, server)

    class Inventory:
        def call(self, action, **params):
            if action == "probe":
                return {
                    "received_at": time.time(),
                    "cpu_count": 32,
                    "cpu_available": 32,
                    "ram_available_mib": 64000,
                    "gpu_probe_error": None,
                    "gpus": [
                        {
                            "index": i,
                            "uuid": f"GPU-synthetic-{i}",
                            "name": "fixture GPU",
                            "memory_total_mib": 24000,
                            "memory_free_mib": 24000,
                            "utilization": 0,
                            "mig_enabled": False,
                            "processes": [] if i in (0, 1, 3, 7) else [{"pid": 999}],
                        }
                        for i in range(8)
                    ],
                }
            if action == "evidence":
                return transport.call(action, **params)
            if action in ("inspect", "cancel"):
                return {"status": "missing"}
            raise AssertionError("This test must never execute training")

    job = db.submit(
        JobSpec(
            server="local",
            cwd=str(home),
            command="python3 train.py --devices 4,5,6,7 --nproc 4 --output metrics.json",
            context_files=["train.py"],
            resources=Resources(gpu_count=4),
            archive=False,
        )
    )
    scheduler = Scheduler(home, settings, transport_factory=lambda *_: Inventory())
    await scheduler.host_tick(server)
    estimate = db.start_agent(job["id"], "estimate", settings.model)
    await scheduler.analyze(db.job(job["id"]), "estimate", estimate)
    assert db.job(job["id"])["status"] == "queued", db.detail(job["id"])["agents"]
    await scheduler.host_tick(server)
    current = db.job(job["id"])
    agent = db.start_agent(job["id"], "launch", settings.model)
    await scheduler.analyze(current, "launch", agent)
    try:
        detail = db.detail(job["id"])
        assert detail["launch_status"] == "completed", detail["agents"]
        run = detail["runs"][0]
        assert [gpu["index"] for gpu in run["allocation"]] == [0, 1, 3, 7]
        args = shlex.split(run["launch_plan"]["command"])
        assert args[args.index("--devices") + 1] == "0,1,2,3"
        assert args[args.index("--nproc") + 1] == "4"
        assert args[args.index("--output") + 1] == "metrics.json"
        expected = shlex.split(job["spec"]["command"])
        expected[expected.index("--devices") + 1] = "0,1,2,3"
        assert args == expected, "Launch should only change the device mapping"
        assert run["launch_plan"]["files"] == [] and run["launch_plan"]["env"] == []
        result = {
            "verified": True,
            "model": settings.model,
            "synthetic_telemetry": True,
            "physical_gpus": [0, 1, 3, 7],
            "logical_gpus": [0, 1, 2, 3],
            "plan": run["launch_plan"],
            "threads": [agent["thread_id"] for agent in detail["agents"]],
            "training_executed": False,
        }
        private_write(home / "verification.json", json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        db.cancel(job["id"])
        await scheduler.reconcile(server, db.job(job["id"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    asyncio.run(run(parser.parse_args().home.resolve()))
