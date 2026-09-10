"""Real Luna feedback for two CPU metric fixtures, including overwritten output paths."""

import argparse
import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import JobSpec, Server
from deepqueue.scheduler import Scheduler

FIXTURE = """import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--accuracy", type=float, required=True)
args = p.parse_args()
records = []
for epoch, acc in enumerate([0.60, args.accuracy - 0.01, args.accuracy], 1):
    row = {"epoch": epoch, "validation_accuracy": acc, "validation_loss": 1 - acc}
    records.append(row)
    print(json.dumps(row), flush=True)
Path("metrics.json").write_text(json.dumps({"synthetic": True, "records": records}))
"""


async def run(home):
    if home.exists():
        raise ValueError("Choose a new diagnostic directory")
    settings = initialize(home)
    settings.agent_socket = "auto"
    settings.model = "gpt-5.6-luna"
    settings.agent_max_attempts = 1
    db = Database(home)
    db.initialize()
    server = Server(name="local", worker_root=str(home / "worker"))
    db.add_server(server)
    private_write(home / "experiment.py", FIXTURE)
    scheduler = Scheduler(home, settings)
    jobs = []
    for accuracy in (0.72, 0.77):
        job = db.submit(
            JobSpec(
                server="local",
                cwd=str(home),
                title="Luna feedback metric fixture",
                command=shlex.join([sys.executable, "experiment.py", "--accuracy", str(accuracy)]),
                context_files=["experiment.py"],
                artifact_files=["metrics.json"],
                resources={"ram_mib": 128},
                skip_estimate=True,
                launch_agent=False,
                tmux=False,
                intent="这是合成指标的 CPU 测试，不是真实模型训练。"
                "请用中文分析 validation_accuracy "
                "和 validation_loss 的初值、最优值、终值及与上次的差异，并说明改进方向与证据限制。",
                parameters={"accuracy": accuracy},
            )
        )
        db.reserve(job["id"], [])
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                await scheduler.reconcile(server, db.job(job["id"]))
                if db.job(job["id"])["status"] in ("succeeded", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert db.job(job["id"])["status"] == "succeeded", db.detail(job["id"])
        finally:
            if db.job(job["id"])["status"] in ("starting", "running", "lost"):
                db.cancel(job["id"])
                await scheduler.reconcile(server, db.job(job["id"]))
        agent = db.start_agent(job["id"], "archive", settings.model)
        await scheduler.analyze(db.job(job["id"]), "archive", agent)
        detail = db.detail(job["id"])
        assert detail["archive_status"] == "completed", detail["agents"]
        jobs.append(detail)
    comparison = jobs[-1]["analysis"]["comparison"]
    assert comparison["job_id"] == jobs[0]["id"]
    agent = jobs[-1]["agents"][-1]
    data = json.loads((home / "agents" / agent["id"] / "evidence.json").read_text())
    assert data["comparison"]["evidence_source"] == "saved_archive_evidence"
    assert "0.72" in json.dumps(data["comparison"]["artifacts"])
    assert "0.77" in json.dumps(data["artifacts"])
    response = json.dumps(jobs[-1]["analysis"], ensure_ascii=False)
    assert "0.77" in response or "77%" in response
    assert "0.72" in response or "72%" in response
    assert "0.05" in response or "5 个百分点" in response or "5个百分点" in response
    verification = {
        "verified": True,
        "model": settings.model,
        "synthetic_metrics": True,
        "actual_cpu_runs": 2,
        "baseline_job_id": jobs[0]["id"],
        "job_id": jobs[-1]["id"],
        "threads": [job["agents"][-1]["thread_id"] for job in jobs],
        "analysis": jobs[-1]["analysis"],
    }
    private_write(
        home / "verification.json", json.dumps(verification, ensure_ascii=False, indent=2)
    )
    print(json.dumps(verification, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    asyncio.run(run(parser.parse_args().home.resolve()))
