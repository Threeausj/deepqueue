"""Real Luna skill submission, targeted toy-model improvement and bounded continuation."""

import argparse
import asyncio
import json
import shlex
import time
from pathlib import Path

from deepqueue.agent import AppServer
from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import Server
from deepqueue.scheduler import Scheduler
from deepqueue.transport import Transport

TRAINING = """import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
args = parser.parse_args()
train_x = [-2., -1., 0., 1., 2.]
train_y = [2 * x + 3 for x in train_x]
weight = 0.
for epoch in range(100):
    errors = [weight * x - y for x, y in zip(train_x, train_y)]
    weight -= 0.05 * 2 * sum(e * x for e, x in zip(errors, train_x)) / len(train_x)
validation_x = [-1.5, 0.5, 2.5]
validation_mse = sum((weight * x - (2 * x + 3)) ** 2 for x in validation_x) / 3
result = {"synthetic_test": True, "validation_mse": validation_mse, "weight": weight,
          "model": "linear_without_intercept", "training_examples": len(train_x)}
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result))
with Path("executions.txt").open("a") as log:
    log.write(str(output) + "\\n")
print(json.dumps(result), flush=True)
"""


async def run(home, sandbox):
    if home.exists():
        raise ValueError("Choose a new local directory")
    settings = initialize(home)
    settings.agent_socket = "auto"
    settings.agent_max_attempts = 1
    settings.agent_timeout_seconds = 240
    settings.poll_seconds = 0.3
    private_write(home / "config.json", settings.model_dump_json())
    private_write(home / "train.py", TRAINING)
    db = Database(home)
    db.initialize()
    server = Server(name="local", worker_root=str(home / "worker"), max_running=1)
    db.add_server(server)
    root = Path(__file__).resolve().parents[1]
    cli = [str(root / ".venv/bin/deepqueue"), "--home", str(home)]
    manifest = {
        "server": "local",
        "cwd": str(home),
        "title": "Synthetic intercept baseline",
        "command": "python3 train.py --output baseline.json",
        "context_files": ["train.py"],
        "artifact_files": ["baseline.json"],
        "resources": {"ram_mib": 128},
        "skip_estimate": True,
        "completion_mode": "improve",
        "max_improvement_rounds": 1,
        "intent": "Run the initial linear model on synthetic y=2x+3 using training data only. "
        "Write this initial result to baseline.json. Preserve training/validation examples. "
        "Any later improvement must use a distinct output. This is a CPU integration test.",
    }
    private_write(home / "job.json", json.dumps(manifest, indent=2))
    async with AppServer(settings, home / "source") as agent:
        started = await agent.request(
            "thread/start",
            {
                "model": settings.model,
                "cwd": str(home),
                "approvalPolicy": "never",
                "sandbox": sandbox,
                "developerInstructions": (
                    "You are participating in an authorized isolated DeepQueue CPU test. "
                    "Only read/edit this test directory and read the supplied DeepQueue skill. "
                    "Do not start a daemon or run training directly. Submit only the initial "
                    "job now; after its completion callback in improve mode, diagnose the error, "
                    "make a targeted model change, validate syntax, and submit exactly one child. "
                    "Use a new child output path. Preserve the synthetic data and protocol. "
                    "Finish-mode callbacks only summarize. Never create unrelated experiments."
                ),
            },
        )
        source = started["thread"]["id"]
        submit = shlex.join(
            cli + ["job", "submit", "--file", str(home / "job.json"), "--from-agent"]
        )
        turn = await agent.request(
            "turn/start",
            {
                "threadId": source,
                "input": [
                    {
                        "type": "skill",
                        "name": "deepqueue",
                        "path": str(root / "src/deepqueue/skill/SKILL.md"),
                    },
                    {
                        "type": "text",
                        "text": (
                            "使用 deepqueue skill 提交已准备的 job.json，"
                            "保留默认启动 agent 和 tmux。"
                            "允许初始实验结束后针对模型误差进行 1 轮改进，但本轮只提交初始任务，"
                            "不修改训练代码、不运行训练、不启动调度器。"
                            "skip_estimate=true 是本次 CPU 测试的明确设置。"
                            "提交后报告真实 ID 并结束本轮。执行：\n" + submit
                        ),
                    },
                ],
            },
        )
        answer = await asyncio.wait_for(agent.wait_for_turn(source, turn["turn"]["id"]), 180)
        private_write(home / "submission.md", answer)
    jobs = db.jobs()
    assert len(jobs) == 1, "Expected exactly one initial skill submission"
    parent_id = jobs[0]["id"]
    assert jobs[0]["spec"]["source_thread_id"] == source
    print(json.dumps({"source": source, "parent": parent_id, "home": str(home)}), flush=True)
    scheduler = Scheduler(home, settings)
    scheduler.acquire()
    previous = None
    try:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            await scheduler.tick()
            jobs = db.jobs()
            state = [
                (job["id"], job["status"], job["launch_status"], job["archive_status"])
                for job in jobs
            ]
            if state != previous:
                print(json.dumps(state), flush=True)
                previous = state
            if any(
                job["status"] == "needs_review" or job["archive_status"] == "failed" for job in jobs
            ):
                raise AssertionError(json.dumps([db.detail(job["id"]) for job in jobs]))
            if len(jobs) == 2 and all(job["archive_status"] == "completed" for job in jobs):
                break
            if len(jobs) == 1 and jobs[0]["archive_status"] == "completed":
                raise AssertionError("Source summarized without submitting an improvement")
            await asyncio.sleep(settings.poll_seconds)
        await scheduler.tick()
        jobs = db.jobs()
        assert len(jobs) == 2
        parent, child = [db.detail(job["id"]) for job in jobs]
        assert all(
            job["status"] == "succeeded" and job["archive_status"] == "completed" for job in jobs
        )
        assert child["spec"]["parent_job_id"] == parent_id
        assert child["priority"] == 100 and child["spec"]["improvement_round"] == 1
        assert child["completion_mode"] == "finish"
        assert len((home / "executions.txt").read_text().splitlines()) == 2
        baseline = json.loads((home / "baseline.json").read_text())
        metrics = [
            json.loads((home / path).read_text())
            for path in child["spec"]["artifact_files"]
            if path.endswith(".json")
        ]
        improved = next(item for item in metrics if "validation_mse" in item)
        assert improved["validation_mse"] < baseline["validation_mse"]
        assert (home / "train.py").read_text() != TRAINING or child["spec"]["context_files"] != [
            "train.py"
        ]
        assert not db.active_runs("local") and not db.leases("local")
        async with AppServer(settings, home / "verify") as agent:
            thread = await agent.read_thread(source)
        for job in (parent, child):
            archive = [item for item in job["agents"] if item["phase"] == "archive"][-1]
            launch = [item for item in job["agents"] if item["phase"] == "launch"][-1]
            assert archive["thread_id"] == source and archive["model"] == settings.model
            assert launch["thread_id"] != source and launch["status"] == "completed"
            turn = next(turn for turn in thread["turns"] if turn["id"] == archive["turn_id"])
            assert AppServer.final_text(turn) == job["analysis"]["summary"]
            assert Transport(home, server).terminal(job["run_id"])["available"]
        result = {
            "verified": True,
            "model": settings.model,
            "source": source,
            "parent": parent_id,
            "child": child["id"],
            "execution_count": 2,
            "baseline_validation_mse": baseline["validation_mse"],
            "improved_validation_mse": improved["validation_mse"],
            "tmux_windows": 2,
            "resources_released": True,
        }
        private_write(home / "verification.json", json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        for task in scheduler.agents.values():
            task.cancel()
        await asyncio.gather(*scheduler.agents.values(), return_exceptions=True)
        scheduler.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument(
        "--agent-sandbox",
        choices=["workspace-write", "danger-full-access"],
        default="workspace-write",
    )
    args = parser.parse_args()
    asyncio.run(run(args.home.resolve(), args.agent_sandbox))
