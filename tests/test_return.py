import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed

from deepqueue.agent import AgentDeferred, AppServer, DeliveryUncertain
from deepqueue.cli import bind_origin, execute, parser
from deepqueue.config import update_settings
from deepqueue.db import SCHEMA_VERSION, Database
from deepqueue.models import JobSpec, Resources, Settings
from deepqueue.scheduler import Scheduler

SOURCE = "source-conversation"
SUMMARY = "## Result\n\nCPU experiment finished. Measured score: **0.75**.\n"


class ConversationServer:
    """Persistent protocol fixture; no model inference."""

    def __init__(self):
        self.status = "idle"
        self.turns = []
        self.requests = []
        self.disconnect_after_delivery = False
        self.wrong_resume_id = False
        self.fail_turn = False
        self.busy_after_resume = False
        self.model = "source-model-not-luna"
        self.expected_override = None
        self.effort = "xhigh"
        self.expected_effort = None

    def thread(self):
        return {"id": SOURCE, "status": {"type": self.status}, "turns": self.turns}

    async def handle(self, socket):
        async for raw in socket:
            message = json.loads(raw)
            method, params = message["method"], message.get("params", {})
            self.requests.append(message)
            if method == "initialized":
                continue
            if method == "initialize":
                result = {"userAgent": "protocol-test-fixture"}
                assert params["clientInfo"]["name"] == "deepqueue"
            elif method == "thread/read":
                assert params["threadId"] == SOURCE
                result = {"thread": self.thread()}
            elif method == "thread/resume":
                assert params == {"threadId": SOURCE, "excludeTurns": True}
                result = {
                    "thread": self.thread(),
                    "model": self.model,
                    "reasoningEffort": self.effort,
                }
                if self.wrong_resume_id:
                    result["thread"]["id"] = "wrong-thread"
                if self.busy_after_resume:
                    self.status = "active"
            elif method == "turn/start":
                assert self.status == "idle"
                assert set(params) == {"threadId", "input"} | (
                    {"model"} if self.expected_override else set()
                ) | ({"effort"} if self.expected_effort is not None else set())
                if self.expected_override:
                    assert params["model"] == self.expected_override
                    self.model = params["model"]
                if self.expected_effort is not None:
                    assert params["effort"] == self.expected_effort
                    self.effort = params["effort"]
                assert params["threadId"] == SOURCE
                turn = {
                    "id": f"returned-turn-{len(self.turns) + 1}",
                    "status": "failed" if self.fail_turn else "completed",
                    "error": {"message": "fixture model failure"} if self.fail_turn else None,
                    "items": [
                        {"id": "request", "type": "userMessage", "content": params["input"]},
                        {
                            "id": "summary",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": SUMMARY,
                        },
                    ],
                }
                self.turns.append(turn)
                if self.disconnect_after_delivery:
                    self.disconnect_after_delivery = False
                    await socket.close()
                    return
                await socket.send(
                    json.dumps({"id": message["id"], "result": {"turn": {"id": turn["id"]}}})
                )
                await socket.send(
                    json.dumps(
                        {"method": "turn/completed", "params": {"threadId": SOURCE, "turn": turn}}
                    )
                )
                continue
            else:
                raise AssertionError(f"Unexpected protocol method: {method}")
            await socket.send(json.dumps({"id": message["id"], "result": result}))

    @asynccontextmanager
    async def serve(self, tmp_path):
        path = str(tmp_path / "codex.sock")
        async with unix_serve(self.handle, path, compression=None):
            yield Settings(agent_socket=path, return_agent_socket=path, agent_max_attempts=1)


def delivery(**updates):
    return {
        "thread_id": SOURCE,
        "delivery_id": "delivery-fixture",
        "previously_sent": False,
        **updates,
    }


async def call_return(settings, directory, record, links=None, turns=None, dispatched=None):
    async with AppServer(settings, directory) as client:
        return await client.return_to_thread(
            "Experiment finished. Summarize the measured result.",
            record,
            lambda *args: links.append(args) if links is not None else None,
            lambda turn: turns.append(turn) if turns is not None else None,
            lambda: dispatched.append(True) if dispatched is not None else None,
        )


async def test_returns_to_same_conversation_and_reuses_completed_delivery(tmp_path):
    backend = ConversationServer()
    links, turns, dispatched = [], [], []
    async with backend.serve(tmp_path) as settings:
        text = await call_return(settings, tmp_path / "first", delivery(), links, turns, dispatched)
        backend.status = "active"
        recovered = await call_return(settings, tmp_path / "second", delivery(previously_sent=True))
    assert text == recovered == SUMMARY
    assert links == [(SOURCE, f"codex://threads/{SOURCE}")]
    assert turns == ["returned-turn-1"]
    assert dispatched == [True]
    assert len(backend.turns) == 1
    assert "thread/start" not in [message["method"] for message in backend.requests]


@pytest.mark.parametrize("after_resume", [False, True])
async def test_busy_source_never_receives_a_new_turn(tmp_path, after_resume):
    backend = ConversationServer()
    backend.status = "idle" if after_resume else "active"
    backend.busy_after_resume = after_resume
    async with backend.serve(tmp_path) as settings:
        with pytest.raises(AgentDeferred, match="busy"):
            await call_return(settings, tmp_path / "busy", delivery())
    assert not backend.turns


async def test_disconnect_after_acceptance_recovers_without_duplicate(tmp_path):
    backend = ConversationServer()
    backend.disconnect_after_delivery = True
    dispatched = []
    async with backend.serve(tmp_path) as settings:
        with pytest.raises(ConnectionClosed):
            await call_return(settings, tmp_path / "lost-ack", delivery(), dispatched=dispatched)
        assert dispatched == [True]
        assert (
            await call_return(settings, tmp_path / "recover", delivery(previously_sent=True))
            == SUMMARY
        )
    assert len(backend.turns) == 1


async def test_unknown_delivery_and_wrong_resumed_thread_stop_without_submission(tmp_path):
    backend = ConversationServer()
    async with backend.serve(tmp_path) as settings:
        with pytest.raises(DeliveryUncertain, match="absent"):
            await call_return(settings, tmp_path / "unknown", delivery(previously_sent=True))
        backend.wrong_resume_id = True
        with pytest.raises(DeliveryUncertain, match="different"):
            await call_return(settings, tmp_path / "wrong", delivery())
    assert not backend.turns


def source_job(db, **updates):
    return db.submit(
        JobSpec(
            server="local",
            command="true",
            cwd=str(db.home),
            source_thread_id=SOURCE,
            resources=Resources(ram_mib=128),
            skip_estimate=True,
            **updates,
        )
    )


def finish_job(db, job):
    assert db.reserve(job["id"], [])
    db.finish(job["id"], {"status": "succeeded", "exit_code": 0})
    return db.job(job["id"])


async def test_server_callback_override_returns_improvement_to_its_source(db, tmp_path):
    backend = ConversationServer()
    async with backend.serve(tmp_path) as settings:
        db.set_server_callback("local", settings.return_agent_socket)
        settings.return_agent_socket = str(tmp_path / "wrong-global-socket")
        update_settings(db.home, {"public_url": "https://queue.example.test"})
        job = finish_job(db, source_job(db, completion_mode="improve"))
        scheduler = Scheduler(db.home, settings)
        agent_id = db.start_agent(job["id"], "archive", "source-thread")
        await scheduler.analyze(job, "archive", agent_id)
    detail = db.detail(job["id"])
    assert detail["archive_status"] == "completed"
    assert detail["agents"][-1]["thread_id"] == SOURCE
    assert detail["agents"][-1]["model"] == backend.model
    prompt = next(item for item in backend.requests if item["method"] == "turn/start")["params"][
        "input"
    ][0]["text"]
    assert "deepqueue --url https://queue.example.test --target-server local job submit" in prompt
    assert "deepqueue --home" not in prompt


def test_source_archives_are_serialized_and_deferral_does_not_exhaust_retries(db):
    first, second = [finish_job(db, source_job(db)) for _ in range(2)]
    agent = db.start_agent(first["id"], "archive", "gpt-5.6-luna")
    original = db.agent_delivery(agent)
    assert db.start_agent(second["id"], "archive", "gpt-5.6-luna") is None
    for _ in range(4):
        db.defer_agent(agent, "Source is busy")
        agent = db.start_agent(first["id"], "archive", "gpt-5.6-luna")
        assert db.agent_delivery(agent)["delivery_id"] == original["delivery_id"]
    db.fail_agent(agent, "one actual failure", 2)
    assert db.job(first["id"])["archive_status"] == "pending"
    assert db.start_agent(second["id"], "archive", "gpt-5.6-luna", retry_seconds=30) is None
    assert db.start_agent(second["id"], "archive", "gpt-5.6-luna")


def test_restart_preserves_delivery_marker_and_explicit_retry_starts_new_delivery(db):
    job = finish_job(db, source_job(db))
    agent = db.start_agent(job["id"], "archive", "gpt-5.6-luna")
    db.agent_dispatched(agent)
    original = db.agent_delivery(agent)
    db.recover_agents(1)
    restarted = db.start_agent(job["id"], "archive", "gpt-5.6-luna")
    restored = db.agent_delivery(restarted)
    assert restored["delivery_id"] == original["delivery_id"]
    assert restored["previously_sent"]
    db.fail_agent(restarted, "unknown acceptance", 1, permanent=True)
    db.retry_agent(job["id"], "archive")
    retried = db.agent_delivery(db.start_agent(job["id"], "archive", "gpt-5.6-luna"))
    assert retried["delivery_id"] != original["delivery_id"]
    assert not retried["previously_sent"]


async def test_cpu_completion_returns_evidence_and_preserves_execution_once(db, tmp_path):
    backend = ConversationServer()
    backend.status = "active"
    job = db.submit(
        JobSpec(
            server="local",
            cwd=str(db.home),
            title="Return CPU result",
            launch_agent=False,
            tmux=False,
            command="printf 'run\\n' >> executions.txt; printf '{\"score\":0.75}' > metrics.json",
            artifact_files=["metrics.json"],
            source_thread_id=SOURCE,
            resources=Resources(ram_mib=128),
            skip_estimate=True,
        )
    )
    async with backend.serve(tmp_path) as settings:
        scheduler = Scheduler(db.home, settings)
        assert db.reserve(job["id"], [])
        from deepqueue.models import Server

        server = Server.model_validate(db.server("local")["config"])
        for _ in range(150):
            await scheduler.reconcile(server, db.job(job["id"]))
            if db.job(job["id"])["status"] == "succeeded":
                break
            await asyncio.sleep(0.03)
        assert db.job(job["id"])["status"] == "succeeded"
        agent = db.start_agent(job["id"], "archive", settings.model)
        await scheduler.analyze(db.job(job["id"]), "archive", agent)
        assert db.last_agent(job["id"], "archive")["status"] == "deferred"
        assert not db.active_runs("local")
        backend.status = "idle"
        agent = db.start_agent(job["id"], "archive", settings.model)
        await scheduler.analyze(db.job(job["id"]), "archive", agent)
    detail = db.detail(job["id"])
    assert detail["archive_status"] == "completed"
    assert detail["analysis"]["summary"] == SUMMARY
    assert detail["analysis"]["source_thread_id"] == SOURCE
    assert detail["agents"][-1]["model"] == "source-model-not-luna"
    assert detail["analysis"]["outcome"] == "inconclusive"
    assert len(detail["runs"]) == 1
    assert (db.home / "executions.txt").read_text() == "run\n"
    assert "0.75" in backend.turns[0]["items"][0]["content"][0]["text"]
    assert SOURCE in (db.home / "archives" / f"{job['id']}.md").read_text()


@pytest.mark.parametrize(
    ("selection", "default", "override"),
    [
        (None, None, None),
        ("source-thread", "queue-archive", None),
        (None, "queue-archive", "queue-archive"),
        ("job-archive", "queue-archive", "job-archive"),
    ],
)
async def test_archive_model_selection_and_recovery(db, tmp_path, selection, default, override):
    backend = ConversationServer()
    backend.expected_override = override
    backend.disconnect_after_delivery = True
    job = finish_job(db, source_job(db, agent_models={"archive": selection}))
    # The conversation model can change while the experiment waits or runs.
    backend.model = "changed-source-model"
    async with backend.serve(tmp_path) as settings:
        settings.agent_models.archive = default
        scheduler = Scheduler(db.home, settings)
        scheduler.start_agents()
        await asyncio.gather(*scheduler.agents.values())
        assert db.job(job["id"])["archive_status"] == "failed"
        last = db.last_agent(job["id"], "archive")
        expected = override or "changed-source-model"
        assert last["model"] == expected
        # Recover the accepted reply with the original delivery marker, even if defaults change.
        with db.connection(write=True) as con:
            con.execute("UPDATE jobs SET archive_status='pending' WHERE id=?", (job["id"],))
        settings.agent_models.archive = "changed-queue-default"
        agent = db.start_agent(job["id"], "archive", "changed-queue-default")
        await scheduler.analyze(db.job(job["id"]), "archive", agent)
    assert db.job(job["id"])["archive_status"] == "completed"
    assert db.last_agent(job["id"], "archive")["model"] == expected
    assert len(backend.turns) == 1
    assert len(db.detail(job["id"])["runs"]) == 1
    assert "thread/start" not in [request["method"] for request in backend.requests]


@pytest.mark.parametrize(
    ("selection", "default", "expected"),
    [
        (None, None, None),
        ("source-thread", "high", None),
        (None, "low", "low"),
        ("medium", "high", "medium"),
        ("none", "high", "none"),
    ],
)
async def test_source_effort_overrides_and_inheritance(db, tmp_path, selection, default, expected):
    backend = ConversationServer()
    backend.expected_effort = expected
    backend.disconnect_after_delivery = True
    job = finish_job(db, source_job(db, agent_efforts={"archive": selection}))
    backend.effort = "high"
    async with backend.serve(tmp_path) as settings:
        settings.agent_efforts.archive = default
        scheduler = Scheduler(db.home, settings)
        scheduler.start_agents()
        await asyncio.gather(*scheduler.agents.values())
        assert db.job(job["id"])["archive_status"] == "failed"
        assert db.last_agent(job["id"], "archive")["effort"] == (expected or "high")
        with db.connection(write=True) as con:
            con.execute("UPDATE jobs SET archive_status='pending' WHERE id=?", (job["id"],))
        agent_id = db.start_agent(job["id"], "archive", "source-thread", effort="changed-default")
        await scheduler.analyze(db.job(job["id"]), "archive", agent_id)
    assert db.job(job["id"])["archive_status"] == "completed"
    assert db.last_agent(job["id"], "archive")["effort"] == (expected or "high")
    assert len(backend.turns) == 1


async def test_unavailable_server_still_returns_recorded_failure(db):
    class OfflineTransport:
        def __init__(self, *_):
            pass

        def call(self, *_args, **_kwargs):
            raise ConnectionError("server disconnected")

    job = finish_job(db, source_job(db))
    evidence = await Scheduler(db.home, Settings(), transport_factory=OfflineTransport).evidence(
        job, "archive"
    )
    assert evidence["execution"]["result"]["exit_code"] == 0
    assert evidence["artifacts"]["unavailable"]
    assert evidence["log_tail"]["unavailable"]


def test_agent_cli_binds_job_and_matrix_to_real_thread_only_when_requested(
    db, monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEX_THREAD_ID", SOURCE)
    root = ["--home", str(db.home)]
    job = execute(
        parser().parse_args(root + ["job", "submit", "--command", "true", "--from-agent"])
    )
    assert job["spec"]["source_thread_id"] == SOURCE
    manual = execute(parser().parse_args(root + ["job", "submit", "--command", "true"]))
    assert manual["spec"]["source_thread_id"] is None
    assert (
        execute(parser().parse_args(root + ["job", "links", job["id"]]))[0]["thread_id"] == SOURCE
    )
    manifest = {
        "name": "agent-matrix",
        "defaults": {"server": "local", "cwd": str(db.home)},
        "matrix": {"seed": [1, 2]},
        "experiments": [{"key": "run-{index}", "command": "true"}],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    args = parser().parse_args(root + ["batch", "submit", str(path), "--from-agent"])
    batch = execute(args)
    assert all(
        db.job(job_id)["spec"]["source_thread_id"] == SOURCE for job_id in batch["jobs"].values()
    )
    assert execute(args)["existing"]
    monkeypatch.setenv("CODEX_THREAD_ID", "another-thread")
    with pytest.raises(ValueError, match="different"):
        execute(args)
    monkeypatch.delenv("CODEX_THREAD_ID")
    with pytest.raises(ValueError, match="missing"):
        execute(args)


def test_origin_rejects_missing_conflicting_and_disabled_return(monkeypatch):
    args = parser().parse_args(["job", "submit", "--from-agent"])
    monkeypatch.setenv("CODEX_THREAD_ID", SOURCE)
    with pytest.raises(ValueError, match="conflicts"):
        bind_origin({"source_thread_id": "other"}, args)
    with pytest.raises(ValueError, match="requires archive"):
        JobSpec(server="local", command="true", cwd="/tmp", source_thread_id=SOURCE, archive=False)
    args = parser().parse_args(["job", "submit", "--source-thread-id", SOURCE])
    assert bind_origin({}, args)["source_thread_id"] == SOURCE


def test_v2_migration_keeps_existing_experiment_and_agent_records(db):
    job = db.submit(JobSpec(server="local", command="true", cwd=str(db.home)))
    agent = db.start_agent(job["id"], "estimate", "gpt-5.6-luna")
    db.agent_link(agent, "estimate-thread", "codex://threads/estimate-thread")
    before = db.detail(job["id"])
    with db.connection(write=True) as con:
        con.execute("DROP INDEX jobs_parent")
        con.execute("DROP TABLE resource_holds")
        con.execute("ALTER TABLE jobs DROP COLUMN completion_mode")
        con.execute("ALTER TABLE jobs DROP COLUMN launch_status")
        con.execute("ALTER TABLE runs DROP COLUMN launch_plan")
        con.execute("ALTER TABLE agent_runs DROP COLUMN delivery_id")
        con.execute("ALTER TABLE agent_runs DROP COLUMN request_sent")
        con.execute("PRAGMA user_version=2")
    Database(db.home).ensure_schema()
    assert db.schema_version() == SCHEMA_VERSION
    assert db.detail(job["id"]) == before
