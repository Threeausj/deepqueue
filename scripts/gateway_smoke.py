"""Exercise the web gateway with local Luna and return a real CPU fixture to its source task."""

import argparse
import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

import httpx

from deepqueue.agent import AppServer
from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import CodexConnection, JobSpec, Server
from deepqueue.scheduler import Scheduler
from deepqueue.web import create_app


async def run(home):
    if home.exists():
        raise ValueError("Choose a fresh test directory")
    settings = initialize(home)
    settings.agent_timeout_seconds = 120
    settings.return_agent_socket = str(home / "unreachable-global.sock")
    private_write(home / "config.json", settings.model_dump_json())
    db = Database(home)
    db.initialize()
    server = Server(
        name="local",
        worker_root=str(home / "worker"),
        codex=CodexConnection(enabled=True, cwd=str(home)),
    )
    db.add_server(server)
    app = create_app(home)
    base = "/api/servers/local/codex"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-DeepQueue": "1"},
        timeout=120,
    ) as client:
        try:
            response = await client.post(base + "/connect", json={})
            response.raise_for_status()
            created = await client.post(
                base + "/threads", json={"cwd": str(home), "model": settings.model}
            )
            created.raise_for_status()
            tid = created.json()["thread"]["id"]
            session = await app.state.codex.session("local")
            events = asyncio.Queue(maxsize=256)
            session.subscribers.add(events)
            sent = await client.post(
                f"{base}/threads/{tid}/messages",
                json={
                    "text": "This is an isolated DeepQueue gateway test. "
                    "Reply exactly DQ_GATEWAY_LUNA_OK. Do not call tools.",
                    "model": settings.model,
                    "effort": "low",
                    "request_id": "luna-gateway-hello",
                },
            )
            assert sent.is_success, sent.text

            async def completion():
                while True:
                    event = await events.get()
                    if (
                        event["method"] == "turn/completed"
                        and event.get("params", {}).get("threadId") == tid
                    ):
                        return event

            await asyncio.wait_for(completion(), 120)
            thread = (await client.get(f"{base}/threads/{tid}")).json()["thread"]
            assert "DQ_GATEWAY_LUNA_OK" in AppServer.final_text(thread["turns"][-1])
            print(
                json.dumps({"phase": "web-turn", "model": settings.model, "thread_id": tid}),
                flush=True,
            )
            # A closed browser gateway must not be required for the scheduler's return path.
            await client.post(base + "/disconnect", json={})
            private_write(
                home / "metric_fixture.py",
                "import json\nfrom pathlib import Path\n"
                'value={"synthetic":True,"validation_accuracy":0.77,"validation_loss":0.23}\n'
                "print(json.dumps(value),flush=True)\n"
                'Path("metrics.json").write_text(json.dumps(value))\n',
            )
            job = db.submit(
                JobSpec(
                    server="local",
                    cwd=str(home),
                    title="Gateway CPU feedback fixture",
                    command=shlex.join([sys.executable, "metric_fixture.py"]),
                    resources={"ram_mib": 128},
                    skip_estimate=True,
                    launch_agent=False,
                    tmux=False,
                    source_thread_id=tid,
                    artifact_files=["metrics.json"],
                    intent="这是合成指标的 CPU 集成测试，不是真实模型训练。"
                    "请简短总结验证精度和损失，明确是合成数据。",
                )
            )
            scheduler = Scheduler(home, settings)
            db.reserve(job["id"], [])
            deadline = time.monotonic() + 30
            try:
                while time.monotonic() < deadline:
                    await scheduler.reconcile(server, db.job(job["id"]))
                    if db.job(job["id"])["status"] in ("succeeded", "failed"):
                        break
                    await asyncio.sleep(0.1)
                assert db.job(job["id"])["status"] == "succeeded"
            finally:
                if db.job(job["id"])["status"] in ("starting", "running", "lost"):
                    db.cancel(job["id"])
                    await scheduler.reconcile(server, db.job(job["id"]))
            agent_id = db.start_agent(job["id"], "archive", "source-thread")
            await scheduler.analyze(db.job(job["id"]), "archive", agent_id)
            detail = db.detail(job["id"])
            assert detail["archive_status"] == "completed", detail["agents"]
            assert detail["agents"][-1]["model"] == settings.model
            await client.post(base + "/connect", json={})
            result = (await client.post(f"{base}/threads/{tid}/open", json={})).json()["thread"]
            assert len(result["turns"]) == 2
            summary = AppServer.final_text(result["turns"][-1])
            assert "0.77" in summary or "77%" in summary
            report = {
                "verified": True,
                "model": settings.model,
                "thread_id": tid,
                "actual_cpu_runs": 1,
                "synthetic_metrics": True,
                "archive_returned_to_same_thread": True,
                "gateway_reconnected": True,
                "job_id": job["id"],
                "summary": summary,
            }
            private_write(
                home / "verification.json", json.dumps(report, ensure_ascii=False, indent=2)
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            await app.state.codex.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    asyncio.run(run(parser.parse_args().home.resolve()))
