"""Exercise real Luna OOM repair with synthetic GPU telemetry and a CPU diagnostic."""

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import TERMINAL, JobSpec, Server
from deepqueue.scheduler import Scheduler
from deepqueue.transport import Transport

FIXTURE = """import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--devices", required=True)
parser.add_argument("--nproc", type=int, required=True)
parser.add_argument("--batch-size", type=int, required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
# Synthetic launcher interface: no CUDA allocation or real model training.
record = dict(vars(args), run_id=os.environ["DEEPQUEUE_RUN_ID"],
              visible=os.environ["CUDA_VISIBLE_DEVICES"])
with Path("attempts.jsonl").open("a") as f:
    f.write(json.dumps(record) + "\\n")
print(json.dumps(record), flush=True)
if args.devices != "0,1,2,3" or args.nproc != 4:
    print("RuntimeError: CUDA error: invalid device ordinal", flush=True)
    sys.exit(2)
if args.batch_size > 2:
    print("torch.cuda.OutOfMemoryError: CUDA out of memory. Diagnostic batch size "
          "must be at most 2; lowering --batch-size is supported.", flush=True)
    sys.exit(1)
Path(args.output).write_text(json.dumps(dict(record, status="success", synthetic=True)))
print("diagnostic_accuracy=0.875 synthetic=true", flush=True)
"""


async def run(home):
    if home.exists():
        raise ValueError("Choose a new diagnostic directory")
    settings = initialize(home)
    settings.agent_socket = "auto"
    settings.poll_seconds = 0.3
    settings.agent_retry_seconds = 1
    private_write(home / "config.json", settings.model_dump_json(indent=2))
    private_write(home / "diagnostic.py", FIXTURE)
    db = Database(home)
    db.initialize()
    server = Server(name="local", worker_root=str(home / "worker"))
    db.add_server(server)
    worker = Transport(home, server)

    class Inventory:
        def call(self, action, **params):
            if action != "probe":
                return worker.call(action, **params)
            return {
                "cpu_count": 64,
                "cpu_available": 64,
                "ram_available_mib": 128000,
                "gpu_probe_error": None,
                "received_at": time.time(),
                "gpus": [
                    {
                        "index": i,
                        "uuid": f"GPU-synthetic-{i}",
                        "name": "synthetic GPU",
                        "memory_total_mib": 24000,
                        "memory_free_mib": 24000,
                        "utilization": 0,
                        "mig_enabled": False,
                        "processes": [],
                    }
                    for i in (0, 1, 3, 7)
                ],
            }

    job = db.submit(
        JobSpec(
            server="local",
            cwd=str(home),
            title="Synthetic automatic OOM repair",
            command=shlex.quote(sys.executable) + " diagnostic.py --devices 4,5,6,7 "
            "--nproc 4 --batch-size 8 --output metrics.json",
            resources={"gpu_count": 4},
            skip_estimate=True,
            context_files=["diagnostic.py"],
            artifact_files=["metrics.json", "attempts.jsonl"],
            intent="Exercise automatic launch and observed OOM recovery. This fixture uses "
            "synthetic GPU metadata and a CPU program; its accuracy is not a scientific result. "
            "Preserve the script's supported CLI and output file, adjust device arguments first, "
            "and change batch size only after the diagnostic emits an OOM.",
            timeout_seconds=30,
        )
    )
    scheduler = Scheduler(home, settings, transport_factory=lambda *_: Inventory())
    scheduler.acquire()
    deadline = time.monotonic() + 600
    last = None
    try:
        while time.monotonic() < deadline:
            await scheduler.tick()
            current = db.job(job["id"])
            state = (
                current["status"],
                current["launch_status"],
                current["launch_retries"],
                current["archive_status"],
            )
            if state != last:
                print(json.dumps({"state": state}), flush=True)
                last = state
            if current["status"] in ("failed", "lost", "needs_review"):
                raise RuntimeError(json.dumps(db.detail(job["id"]), ensure_ascii=False))
            if current["status"] == "succeeded" and current["archive_status"] == "completed":
                detail = db.detail(job["id"])
                attempts = [
                    json.loads(line) for line in (home / "attempts.jsonl").read_text().splitlines()
                ]
                assert 1 <= current["launch_retries"] <= settings.launch_max_retries
                assert len(attempts) == len(detail["runs"])
                assert len({attempt["run_id"] for attempt in attempts}) == len(attempts)
                assert attempts[0]["batch_size"] == 8
                assert attempts[-1]["batch_size"] <= 2
                assert all(
                    attempt["devices"] == "0,1,2,3" and attempt["nproc"] == 4
                    for attempt in attempts
                )
                assert all(agent["model"] == "gpt-5.6-luna" for agent in detail["agents"])
                assert all(worker.terminal(run["id"])["available"] for run in detail["runs"])
                assert not db.leases("local")
                report = {
                    "verified": True,
                    "synthetic_telemetry": True,
                    "real_gpu_oom": False,
                    "model": settings.model,
                    "attempts": attempts,
                    "job": detail,
                }
                private_write(
                    home / "verification.json", json.dumps(report, ensure_ascii=False, indent=2)
                )
                print(
                    json.dumps(
                        {
                            "verified": True,
                            "report": str(home / "verification.json"),
                            "executions": len(attempts),
                        }
                    ),
                    flush=True,
                )
                return
            await asyncio.sleep(settings.poll_seconds)
        raise TimeoutError("Automatic recovery diagnostic exceeded 600 seconds")
    finally:
        for task in scheduler.agents.values():
            task.cancel()
        await asyncio.gather(*scheduler.agents.values(), return_exceptions=True)
        if db.job(job["id"])["status"] not in TERMINAL:
            db.cancel(job["id"])
            for _ in range(30):
                await scheduler.reconcile(server, db.job(job["id"]))
                if db.job(job["id"])["status"] in TERMINAL:
                    break
                await asyncio.sleep(0.2)
        for attempt in db.detail(job["id"])["runs"]:
            subprocess.run(
                ["tmux", "kill-session", "-t", "deepqueue-" + attempt["id"]], capture_output=True
            )
        scheduler.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    asyncio.run(run(parser.parse_args().home.resolve()))
