"""Isolated browser fixture: real CPU output plus an explicitly labelled UI archive fixture."""

import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from codex_activity_fixture import activity
from codex_fixture import CodexFixture
from websockets.asyncio.server import unix_serve
from workspace_fixture import add_workspace, preview_service

from deepqueue.config import initialize, private_write
from deepqueue.db import Database
from deepqueue.models import CodexConnection, JobSpec, LaunchPlan, Resources, Server
from deepqueue.transport import Transport
from deepqueue.web import create_app

home = Path(tempfile.mkdtemp(prefix="deepqueue-browser-"))
settings = initialize(home)
settings.poll_seconds = 0.2
private_write(home / "config.json", settings.model_dump_json())
db = Database(home)
db.initialize()
server = Server(name="local", worker_root=str(home / "worker"), max_running=2)
db.add_server(server)
db.add_server(Server(name="remote-ui", worker_root=str(home / "remote-worker"), enabled=False))
shutil.copy2(Path(__file__).resolve().parents[1] / "examples/cpu_experiment.py", home)
worker = Transport(home, server)
snapshot = worker.call("probe")
snapshot["received_at"] = time.time()
db.snapshot("local", snapshot)
submitted = db.submit_batch(
    {
        "name": "browser-cpu-sweep",
        "defaults": {
            "server": "local",
            "cwd": str(home),
            "resources": {"ram_mib": 128},
            "skip_estimate": True,
            "launch_agent": False,
            "tmux": False,
            "archive": False,
        },
        "matrix": {"lr": [0.001, 0.003, 0.01], "seed": [7, 11]},
        "experiments": [
            {
                "key": "trial-{index}",
                "title": "CPU baseline / lr {lr} / seed {seed}",
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
)
first = db.job(submitted["jobs"]["trial-1"])
run_id = db.reserve(first["id"], [])
worker.call(
    "launch",
    run_id=run_id,
    spec={
        "cwd": str(home),
        "command": first["spec"]["command"],
        "env": {},
        "gpu_uuids": [],
        "timeout_seconds": 10,
        "tmux": True,
    },
)
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    result = worker.call("inspect", run_id=run_id)
    if result["status"] == "succeeded":
        db.finish(first["id"], result)
        break
    time.sleep(0.1)
else:
    worker.call("cancel", run_id=run_id)
    raise TimeoutError("Browser fixture CPU job did not finish")

# This structured record exercises rendering only. It is not a model inference result.
db.retry_agent(first["id"], "archive")
agent = db.start_agent(first["id"], "archive", "ui-test-fixture")
db.agent_link(
    agent,
    "00000000-0000-0000-0000-000000000001",
    "codex://threads/00000000-0000-0000-0000-000000000001",
)
db.complete_agent(
    agent,
    {
        "summary": "UI test fixture: the CPU experiment produced a synthetic optimization result.",
        "outcome": "success",
        "findings": ["The result file and process exit were verified."],
        "resource_assessment": (
            "CPU-only execution; this text is a browser test fixture, not a Luna response."
        ),
        "next_steps": ["Compare the remaining learning-rate combinations."],
        "suggested_command": None,
    },
)
returned = db.submit(
    JobSpec(
        server="local",
        cwd=str(home),
        command="true",
        title="Source conversation UI fixture",
        resources=Resources(ram_mib=128),
        skip_estimate=True,
        source_thread_id="00000000-0000-0000-0000-000000000002",
    )
)
db.cancel(returned["id"])
return_agent = db.start_agent(returned["id"], "archive", "ui-test-fixture")
db.agent_link(
    return_agent,
    returned["spec"]["source_thread_id"],
    "codex://threads/" + returned["spec"]["source_thread_id"],
)
db.complete_agent(
    return_agent,
    {
        "summary": "## 回传总结\n\n这是 **ui-test-fixture**，实验在执行前取消。\n\n"
        "| 结果 | 数值 |\n| --- | --- |\n| 实际执行次数 | 0 |\n",
        "format": "markdown",
        "source_thread_id": returned["spec"]["source_thread_id"],
        "outcome": "cancelled",
        "findings": [],
        "next_steps": [],
        "resource_assessment": "",
        "suggested_command": None,
    },
)
repaired = db.submit(
    JobSpec(
        server="local",
        cwd=str(home),
        title="Automatic recovery UI fixture",
        command="python train.py --batch-size 8",
        resources=Resources(gpu_count=1),
        skip_estimate=True,
        archive=False,
    )
)
allocation = [{"index": 0, "uuid": "GPU-browser-fixture", "name": "UI fixture"}]
old_run = db.reserve(repaired["id"], allocation)
old_agent = db.start_agent(repaired["id"], "launch", "ui-test-fixture")
db.complete_agent(
    old_agent,
    LaunchPlan(
        command=repaired["spec"]["command"], rationale="UI fixture initial plan"
    ).model_dump(),
)
db.running(repaired["id"])
next_run = db.retry_launch(
    repaired["id"],
    old_run,
    {"status": "failed", "exit_code": 1},
    "oom",
    3,
)
next_agent = db.start_agent(repaired["id"], "launch", "ui-test-fixture")
db.complete_agent(
    next_agent,
    LaunchPlan(
        command="python train.py --batch-size 2", rationale="UI fixture: batch 8 to 2"
    ).model_dump(),
)
db.finish(repaired["id"], {"status": "succeeded", "exit_code": 0})
private_write(home / "worker/runs" / old_run / "output.log", "UI fixture: CUDA out of memory\n")
private_write(
    home / "worker/runs" / next_run / "output.log", "UI fixture: recovered batch_size=2\n"
)
print(f"Browser fixture home: {home}", flush=True)
fixture = CodexFixture("local")
fixture.add_project("vision", "视觉实验", ["/tmp/codex-fixture"])
fixture.add_project("empty-project", "待开始的项目", ["/tmp/empty-project"])
fixture.threads["local-task"]["projectId"] = "vision"
fixture.add_thread("standalone-task", "独立分析任务", cwd="/tmp/codex-fixture")
db.set_server_codex("local", CodexConnection(socket=str(home / "codex.sock"), cwd=str(home)))
app = create_app(home)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def fixture_lifespan(app):
    async with unix_serve(fixture.handle, str(home / "codex.sock"), compression=None):
        with preview_service() as preview_port:
            app.state.preview_port = preview_port
            async with original_lifespan(app):
                yield
        await fixture.close()


@app.post("/api/fixture/workspace")
def workspace_fixture():
    return {**add_workspace(fixture, home / "workspace-project"), "port": app.state.preview_port}


@app.post("/api/fixture/activity")
async def activity_fixture(action: str = "setup"):
    return await activity(fixture, home / "workspace-project", action)


@app.post("/api/fixture/namespace-failure")
def namespace_failure(enabled: bool = True):
    fixture.namespace_failure = enabled
    return {"ok": True}


@app.get("/api/fixture/codex-calls")
def fixture_calls():
    return fixture.calls


app.router.lifespan_context = fixture_lifespan
uvicorn.run(app, host="127.0.0.1", port=18765)
