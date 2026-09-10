"""Real Luna skill submission over HTTP, isolated SSH execution, and source return."""

import argparse
import asyncio
import importlib.util
import json
import shlex
import socket
import sys
import time
from pathlib import Path

import uvicorn

from deepqueue.access import Access
from deepqueue.agent import AppServer, socket_path
from deepqueue.config import Secrets, initialize, private_write
from deepqueue.db import Database
from deepqueue.models import Server
from deepqueue.scheduler import Scheduler
from deepqueue.skills import install_skill
from deepqueue.transport import trust_host
from deepqueue.web import create_app

ROOT = Path(__file__).resolve().parents[1]


async def run(home):
    if home.exists():
        raise ValueError("Choose a new isolated directory on a local filesystem")
    settings = initialize(home)
    settings.agent_socket = "auto"
    settings.return_agent_socket = str(home / "unused-global-callback.sock")
    settings.model = "gpt-5.6-luna"
    settings.agent_effort = "low"
    settings.agent_timeout_seconds = 120
    settings.agent_max_attempts = 1
    settings.poll_seconds = 0.25
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    settings.public_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    private_write(home / "config.json", settings.model_dump_json(indent=2))
    db = Database(home)
    db.initialize()
    Access(home).create("Isolated test administrator")
    spec = importlib.util.spec_from_file_location("ssh_fixture", ROOT / "tests/test_ssh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture_home = home / "ssh-fixture"
    fixture_home.mkdir()
    with module.loopback_ssh(fixture_home) as ssh:
        trust_host(home, "127.0.0.1", ssh["port"], ssh["fingerprint"])
        db.add_server(
            Server(
                name="ssh-cpu",
                kind="ssh",
                host="127.0.0.1",
                port=ssh["port"],
                username="fixture",
                python=sys.executable,
                password_ref=Secrets(home).put("fixture-password"),
                return_agent_socket=socket_path("auto"),
            )
        )
        credential = Access(home).create("Isolated submission client", "ssh-cpu")
        profile = home / "client.json"
        private_write(
            profile,
            json.dumps(
                {
                    "url": settings.public_url,
                    "server": "ssh-cpu",
                    "token": credential["token"],
                }
            ),
        )
        skill = home / "skill/deepqueue"
        install_skill(skill, settings.public_url, "ssh-cpu")
        manifest = home / "batch.json"
        private_write(
            manifest,
            json.dumps(
                {
                    "name": "ssh-cpu-public-smoke",
                    "defaults": {
                        "cwd": str(ssh["root"]),
                        "resources": {"ram_mib": 128},
                        "intent": "Synthetic delivery verification, not model quality evidence.",
                    },
                    "experiments": [
                        {
                            "key": "synthetic",
                            "title": "Public client and SSH verification",
                            "command": "printf 'executed\\n' >> executions.txt; "
                            'printf \'{"synthetic_test":true,"score":0.75}\' > metrics.json',
                            "artifact_files": ["metrics.json"],
                        }
                    ],
                },
                indent=2,
            ),
        )
        web = uvicorn.Server(uvicorn.Config(create_app(home), log_level="error", lifespan="off"))
        serving = asyncio.create_task(web.serve(sockets=[listener]))
        scheduler = Scheduler(home, settings)
        acquired = False
        try:
            deadline = time.monotonic() + 10
            while not web.started and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert web.started
            async with AppServer(settings, home / "source") as agent:
                source = await agent.request(
                    "thread/start",
                    {
                        "model": settings.model,
                        "cwd": str(home),
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                        "developerInstructions": (
                            "This is an isolated DeepQueue development test. Use only the supplied "
                            "skill and exact submission commands. Do not read client.json, print "
                            "credentials, edit the manifest, execute its command, "
                            "or start a daemon. "
                            "For later completion notifications summarize in Chinese without tools."
                        ),
                    },
                )
                source_id = source["thread"]["id"]
                private_write(home / "source-id.json", json.dumps({"thread_id": source_id}))
                prefix = [
                    "env",
                    f"DEEPQUEUE_CLIENT_CONFIG={profile}",
                    str(ROOT / ".venv/bin/deepqueue"),
                    "--url",
                    settings.public_url,
                    "--target-server",
                    "ssh-cpu",
                ]
                commands = [
                    shlex.join(prefix + ["batch", action, str(manifest), "--from-agent"])
                    for action in ("preview", "submit")
                ]
                turn = await agent.request(
                    "turn/start",
                    {
                        "threadId": source_id,
                        "model": settings.model,
                        "effort": "low",
                        "input": [
                            {"type": "skill", "name": "deepqueue", "path": str(skill / "SKILL.md")},
                            {
                                "type": "text",
                                "text": (
                                    "The baseline synthetic score is 0.50. Submit this prepared "
                                    "CPU test through the supplied server-specific skill. "
                                    "Resource estimation and archiving are enabled intentionally. "
                                    "Do not change the manifest. Use the current CODEX_THREAD_ID "
                                    "via --from-agent. Run the exact commands below, "
                                    "report the job ID, and end the turn. "
                                    "Do not wait for execution or read credentials.\n"
                                    + "\n".join(commands)
                                ),
                            },
                        ],
                    },
                )
                answer = await asyncio.wait_for(
                    agent.wait_for_turn(source_id, turn["turn"]["id"]), 240
                )
                private_write(home / "submission.md", answer)
            jobs = db.jobs()
            assert len(jobs) == 1 and jobs[0]["server"] == "ssh-cpu"
            job_id = jobs[0]["id"]
            assert jobs[0]["spec"]["source_thread_id"] == source_id
            assert not any(jobs[0]["spec"]["agent_models"].values())
            scheduler.acquire()
            acquired = True
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                await scheduler.tick()
                job = db.detail(job_id)
                if job["archive_status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(settings.poll_seconds)
            job = db.detail(job_id)
            assert job["status"] == "succeeded" and job["archive_status"] == "completed", job[
                "agents"
            ]
            assert (
                len(job["runs"]) == 1
                and (ssh["root"] / "executions.txt").read_text() == "executed\n"
            )
            assert job["agents"][-1]["thread_id"] == source_id
            assert all(item["model"] == settings.model for item in job["agents"])
            async with AppServer(settings, home / "verify-source") as agent:
                source = await agent.read_thread(source_id)
            archived = next(
                turn for turn in source["turns"] if turn["id"] == job["agents"][-1]["turn_id"]
            )
            assert AppServer.final_text(archived) == job["analysis"]["summary"]
            report = {
                "verified": True,
                "model": settings.model,
                "source_thread_id": source_id,
                "job_id": job_id,
                "server": "ssh-cpu",
                "execution_count": 1,
                "skill_submitted_over_http": True,
                "password_ssh_execution": True,
                "server_callback_override": True,
                "source_history_verified": True,
                "agent_phases": [item["phase"] for item in job["agents"]],
            }
            private_write(home / "verification.json", json.dumps(report, indent=2))
            print(json.dumps(report), flush=True)
        finally:
            for task in scheduler.agents.values():
                task.cancel()
            await asyncio.gather(*scheduler.agents.values(), return_exceptions=True)
            for job in db.jobs(["starting", "running"]):
                db.cancel(job["id"])
                await scheduler.reconcile(
                    Server.model_validate(db.server(job["server"])["config"]), db.job(job["id"])
                )
            if acquired:
                scheduler.release()
            web.should_exit = True
            await serving
            listener.close()


if __name__ == "__main__":
    options = argparse.ArgumentParser()
    options.add_argument("--home", type=Path, required=True)
    asyncio.run(run(options.parse_args().home.resolve()))
