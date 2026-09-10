import asyncio
import time

import pytest

from deepqueue.config import load_settings
from deepqueue.models import Analysis, JobSpec, Resources, Server
from deepqueue.scheduler import Scheduler


class ArchiveAgent:
    def __init__(self, *_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def analyze(self, prompt, schema, on_thread, on_turn):
        assert schema is Analysis
        assert '"exit_code": 0' in prompt
        assert "one-execution" in prompt
        on_thread("archive-fixture", "codex://threads/archive-fixture")
        on_turn("archive-turn")
        return Analysis(
            summary="Command completed",
            outcome="success",
            findings=["exit 0"],
            resource_assessment="CPU only",
            next_steps=["Inspect result"],
            suggested_command=None,
        )


@pytest.mark.parametrize("before_dispatch", [True, False])
async def test_restart_recovers_existing_run_without_reexecution(db, before_dispatch):
    job = db.submit(
        JobSpec(
            server="local",
            cwd=str(db.home),
            command="printf 'one-execution\n' >> count.txt; cat count.txt; sleep 1",
            resources=Resources(ram_mib=128),
            skip_estimate=True,
            artifact_files=["count.txt"],
            launch_agent=False,
            tmux=False,
        )
    )
    first = Scheduler(db.home, load_settings(db.home), agent_factory=ArchiveAgent)
    first.acquire()
    run_id = db.reserve(job["id"], [])
    if not before_dispatch:
        await first.reconcile(
            Server.model_validate(db.server("local")["config"]), db.job(job["id"])
        )
    first.release()

    second = Scheduler(db.home, load_settings(db.home), agent_factory=ArchiveAgent)
    second.acquire()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await second.tick()
            current = db.job(job["id"])
            if current["status"] == "succeeded" and current["archive_status"] == "completed":
                break
            await asyncio.sleep(0.1)
        assert current["status"] == "succeeded"
        assert current["archive_status"] == "completed"
        assert current["run_id"] == run_id
        assert (db.home / "count.txt").read_text() == "one-execution\n"
        assert len(db.detail(job["id"])["runs"]) == 1
        assert (db.home / "archives" / f"{job['id']}.md").exists()
        assert not db.active_runs("local")
    finally:
        for task in second.agents.values():
            task.cancel()
        await asyncio.gather(*second.agents.values(), return_exceptions=True)
        second.release()
