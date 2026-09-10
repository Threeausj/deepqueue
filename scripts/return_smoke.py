"""Verify a real Luna conversation receives a harmless CPU experiment result."""

import argparse
import asyncio
import json
import shlex
import time
from pathlib import Path

from deepqueue.agent import AppServer
from deepqueue.cli import execute, parser
from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import AgentEfforts, AgentModels, Server
from deepqueue.scheduler import Scheduler


async def run(
    home,
    source_thread_id=None,
    *,
    via_agent=False,
    agent_sandbox="workspace-write",
    explicit_stage_models=False,
    global_agent_defaults=False,
):
    if via_agent and source_thread_id:
        raise ValueError("Agent submission tests must create their own diagnostic conversation")
    if home.exists():
        raise ValueError("Choose a new directory on a local filesystem")
    settings = initialize(home)
    settings.agent_socket = "auto"
    settings.agent_max_attempts = 1
    settings.agent_timeout_seconds = 90
    settings.poll_seconds = 0.25
    if global_agent_defaults:
        settings.agent_models = AgentModels(
            estimate=settings.model, launch=settings.model, archive=settings.model
        )
        settings.agent_efforts = AgentEfforts(estimate="low", launch="medium", archive="high")
    private_write(home / "config.json", settings.model_dump_json(indent=2))
    db = Database(home)
    db.initialize()
    db.add_server(Server(name="local", worker_root=str(home / "worker")))
    if source_thread_id is None:
        async with AppServer(settings, home / "source") as agent:
            result = await agent.request(
                "thread/start",
                {
                    "model": settings.model,
                    "cwd": str(home),
                    "approvalPolicy": "never",
                    "sandbox": agent_sandbox if via_agent else "read-only",
                    "developerInstructions": (
                        "Use the supplied DeepQueue skill for the specified isolated submission. "
                        "Do not change any files outside this test queue or start a daemon. "
                        "Only submit the prepared batch when explicitly provided in a user turn. "
                        "Do not invent other experiments. "
                        "On completion notifications, summarize without calling tools."
                    )
                    if via_agent
                    else (
                        "DeepQueue integration test. Use supplied evidence; do not call tools. "
                        "Summarize experiment results in Chinese."
                    ),
                },
            )
            source_thread_id = result["thread"]["id"]
            turn = await agent.request(
                "turn/start",
                {
                    "threadId": source_thread_id,
                    "model": settings.model,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "Reply only: baseline score recorded as 0.50. "
                                "Do not call any tools or submit anything in this turn."
                            ),
                        }
                    ],
                },
            )
            await asyncio.wait_for(agent.wait_for_turn(source_thread_id, turn["turn"]["id"]), 90)
    manifest = {
        "name": "luna-source-return",
        "defaults": {
            "server": "local",
            "cwd": str(home),
            "resources": {"ram_mib": 128},
            "skip_estimate": not (explicit_stage_models or global_agent_defaults),
            **(
                {
                    "agent_models": {
                        phase: settings.model for phase in ("estimate", "launch", "archive")
                    }
                }
                if explicit_stage_models
                else {}
            ),
        },
        "experiments": [
            {
                "key": "synthetic-score",
                "title": "Synthetic source-return verification",
                "command": "printf 'executed\\n' >> executions.txt; "
                'printf \'{"synthetic_test":true,"score":0.75}\' > metrics.json',
                "artifact_files": ["metrics.json"],
                "intent": (
                    "Verify completion delivery, not scientific model quality. "
                    "Compare to the original baseline."
                ),
            }
        ],
    }
    private_write(home / "batch.json", json.dumps(manifest, indent=2))
    if via_agent:
        root = Path(__file__).resolve().parents[1]
        cli = [str(root / ".venv/bin/deepqueue"), "--home", str(home)]
        commands = [
            shlex.join(cli + ["batch", action, str(home / "batch.json"), "--from-agent"])
            for action in ("preview", "submit")
        ]
        skip_estimate = json.dumps(manifest["defaults"]["skip_estimate"])
        async with AppServer(settings, home / "agent-submit") as agent:
            await agent.request(
                "thread/resume", {"threadId": source_thread_id, "excludeTurns": True}
            )
            turn = await agent.request(
                "turn/start",
                {
                    "threadId": source_thread_id,
                    "model": settings.model,
                    "input": [
                        {
                            "type": "skill",
                            "name": "deepqueue",
                            "path": str(root / "src/deepqueue/skill/SKILL.md"),
                        },
                        {
                            "type": "text",
                            "text": (
                                "Use the supplied deepqueue skill to submit the prepared batch. "
                                "This is an authorized isolated CPU test. "
                                f"skip_estimate={skip_estimate} "
                                "is intentional. Keep archive=true; do not edit the manifest. "
                                "Use the actual current CODEX_THREAD_ID through --from-agent. "
                                "Do not start a daemon or execute the experiment yourself. "
                                "Run these preview and submission commands, report the job ID, "
                                "then end the turn so the queue can return the result here:\n"
                                + "\n".join(commands)
                            ),
                        },
                    ],
                },
            )
            answer = await asyncio.wait_for(
                agent.wait_for_turn(source_thread_id, turn["turn"]["id"]), 240
            )
            private_write(home / "submission.md", answer)
        submitted = db.jobs()
        assert len(submitted) == 1, "The real agent did not submit exactly one job"
        assert submitted[0]["spec"]["source_thread_id"] == source_thread_id
    args = [
        "--home",
        str(home),
        "batch",
        "submit",
        str(home / "batch.json"),
        "--source-thread-id",
        source_thread_id,
    ]
    batch = execute(parser().parse_args(args))
    if via_agent:
        assert batch["existing"], "Agent and CLI binding must share submission identity"
    job_id = batch["jobs"]["synthetic-score"]
    print(
        json.dumps({"source_thread_id": source_thread_id, "job_id": job_id, "home": str(home)}),
        flush=True,
    )
    await verify_execution(
        home,
        settings,
        source_thread_id,
        job_id,
        via_agent=via_agent,
        explicit_stage_models=explicit_stage_models,
        global_agent_defaults=global_agent_defaults,
    )


async def verify_execution(
    home,
    settings,
    source_thread_id,
    job_id,
    *,
    via_agent=False,
    explicit_stage_models=False,
    global_agent_defaults=False,
):
    db = Database(home)
    scheduler = Scheduler(home, settings)
    scheduler.acquire()
    deadline = time.monotonic() + 3 * settings.agent_timeout_seconds + 30
    try:
        while time.monotonic() < deadline:
            await scheduler.tick()
            job = db.detail(job_id)
            if job["archive_status"] in ("completed", "failed"):
                break
            await asyncio.sleep(settings.poll_seconds)
        job = db.detail(job_id)
        if job["archive_status"] != "completed":
            raise RuntimeError(json.dumps(job["agents"], ensure_ascii=False))
        assert job["status"] == "succeeded" and len(job["runs"]) == 1
        assert (home / "executions.txt").read_text() == "executed\n"
        assert job["agents"][-1]["thread_id"] == source_thread_id
        if explicit_stage_models:
            assert {agent["phase"] for agent in job["agents"]} == {"estimate", "launch", "archive"}
            assert all(agent["model"] == settings.model for agent in job["agents"])
            assert job["spec"]["agent_models"] == {
                phase: settings.model for phase in ("estimate", "launch", "archive")
            }
        if global_agent_defaults:
            assert not any(job["spec"]["agent_models"].values())
            assert not any(job["spec"]["agent_efforts"].values())
            assert all(agent["model"] == settings.model for agent in job["agents"])
            assert {agent["phase"]: agent["effort"] for agent in job["agents"]} == {
                "estimate": "low",
                "launch": "medium",
                "archive": "high",
            }
        async with AppServer(settings, home / "verify-source") as agent:
            source = await agent.read_thread(source_thread_id)
        archive_turn = job["agents"][-1]["turn_id"]
        turn = next(turn for turn in source["turns"] if turn["id"] == archive_turn)
        assert AppServer.final_text(turn) == job["analysis"]["summary"]
        result = {
            "verified": True,
            "model": settings.model,
            "source_thread_id": source_thread_id,
            "archive_thread_id": job["agents"][-1]["thread_id"],
            "turn_id": archive_turn,
            "execution_count": 1,
            "source_history_verified": True,
            "agent_submitted_via_skill": via_agent,
            "explicit_stage_models": explicit_stage_models,
            "global_agent_defaults": global_agent_defaults,
            "agent_settings": [
                {"phase": agent["phase"], "model": agent["model"], "effort": agent["effort"]}
                for agent in job["agents"]
            ],
            "summary": job["analysis"]["summary"],
        }
        private_write(home / "verification.json", json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        for task in scheduler.agents.values():
            task.cancel()
        await asyncio.gather(*scheduler.agents.values(), return_exceptions=True)
        for job in db.jobs(["starting", "running"]):
            db.cancel(job["id"])
            await scheduler.reconcile(
                Server.model_validate(db.server("local")["config"]), db.job(job["id"])
            )
        scheduler.release()


if __name__ == "__main__":
    options = argparse.ArgumentParser()
    options.add_argument("--home", type=Path, required=True)
    options.add_argument(
        "--agent-sandbox",
        choices=["workspace-write", "danger-full-access"],
        default="workspace-write",
        help="Execution policy for the owned skill-submission test",
    )
    options.add_argument(
        "--via-agent", action="store_true", help="Have real Luna submit using the skill"
    )
    options.add_argument(
        "--source-thread-id", help="An owned diagnostic thread with a 0.50 baseline"
    )
    model_options = options.add_mutually_exclusive_group()
    model_options.add_argument(
        "--explicit-stage-models",
        action="store_true",
        help="Select Luna independently for all stages, including estimation",
    )
    model_options.add_argument(
        "--global-agent-defaults",
        action="store_true",
        help="Inherit global Luna models and per-stage reasoning efforts",
    )
    args = options.parse_args()
    asyncio.run(
        run(
            args.home.resolve(),
            args.source_thread_id,
            via_agent=args.via_agent,
            agent_sandbox=args.agent_sandbox,
            explicit_stage_models=args.explicit_stage_models,
            global_agent_defaults=args.global_agent_defaults,
        )
    )
