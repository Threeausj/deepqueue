import asyncio
import json
import shlex
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from runpy import run_path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_ssh import Auth, loopback_ssh
from websockets.asyncio.server import unix_serve

from deepqueue.access import Access
from deepqueue.agent import AppServer
from deepqueue.cli import execute, parser
from deepqueue.config import Secrets
from deepqueue.gateway import CodexGateway, Message, NewThread, Reply
from deepqueue.models import CodexConnection, Server, Settings
from deepqueue.transport import trust_host
from deepqueue.web import create_app

CodexFixture = run_path(str(Path(__file__).resolve().parents[1] / "scripts/codex_fixture.py"))[
    "CodexFixture"
]


@asynccontextmanager
async def fixture_gateway(db, tmp_path):
    fixture = CodexFixture()
    path = str(tmp_path / "codex.sock")
    db.set_server_codex("local", CodexConnection(enabled=True, socket=path))
    gateway = CodexGateway(db.home, db)
    async with unix_serve(fixture.handle, path, compression=None):
        try:
            yield gateway, fixture
        finally:
            await gateway.close()
            await fixture.close()


async def notification(queue, method):
    async with asyncio.timeout(5):
        while True:
            event = await queue.get()
            if event["method"] == method:
                return event


async def test_shared_connection_fanout_reconnect_and_send_deduplication(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await asyncio.gather(session.connect(), session.connect())
        assert len(fixture.clients) == 1
        first, second = asyncio.Queue(maxsize=256), asyncio.Queue(maxsize=256)
        session.subscribers.update([first, second])
        thread = (await session.open_thread("fixture-task", 40))["thread"]
        assert thread["id"] == "fixture-task"
        msg = Message(text="分析实验", model="gpt-5.6-luna", effort="high", request_id="once")
        responses = await asyncio.gather(
            session.send_message(thread["id"], msg), session.send_message(thread["id"], msg)
        )
        assert responses[0] == responses[1]
        for queue in (first, second):
            event = await notification(queue, "turn/completed")
            assert "协议测试" in event["params"]["turn"]["items"][-1]["text"]
        calls = [c for c in fixture.calls if c.get("method") == "turn/start"]
        assert len(calls) == 1
        assert calls[0]["params"]["effort"] == "high"
        assert all("sandbox" not in c.get("params", {}) for c in fixture.calls)
        with pytest.raises(HTTPException, match="request_id"):
            await session.send_message(thread["id"], msg.model_copy(update={"text": "different"}))
        await session.disconnect()
        assert session.state == "disconnected"
        await session.connect()
        assert len((await session.open_thread(thread["id"], 40))["thread"]["turns"]) == 1
        assert await session.send_message(thread["id"], msg) == responses[0]


@pytest.mark.parametrize("kind", ["审批", "提问"])
async def test_interactive_requests_resolved_once_and_recovered_for_new_viewers(db, tmp_path, kind):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        queue = asyncio.Queue(maxsize=256)
        session.subscribers.add(queue)
        await session.send_message("fixture-task", Message(text=kind, request_id="interactive"))
        method = (
            "item/tool/requestUserInput"
            if kind == "提问"
            else "item/commandExecution/requestApproval"
        )
        event = await notification(queue, method)
        assert session.status()["pending"] == [event]
        invalid = {"decision": "unexpected"} if kind == "审批" else {"answers": {}}
        with pytest.raises(ValueError):
            await session.reply(
                Reply(request_id=event["id"], generation=session.generation, result=invalid)
            )
        result = (
            {"decision": "decline"}
            if kind == "审批"
            else {"answers": {"direction": {"answers": ["验证精度"]}}}
        )
        await session.reply(
            Reply(request_id=event["id"], generation=session.generation, result=result)
        )
        with pytest.raises(HTTPException):
            await session.reply(
                Reply(request_id=event["id"], generation=session.generation, result=result)
            )
        await notification(queue, "turn/completed")
        assert not session.status()["pending"]
        assert fixture.replies[event["id"]].result()["result"] == result


async def test_slow_viewer_resync_includes_pending_requests(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, _):
        session = await gateway.session("local")
        queue = asyncio.Queue(maxsize=2)
        session.subscribers.add(queue)
        session.pending['"req"'] = {"id": "req", "method": "item/tool/requestUserInput"}
        for _ in range(3):
            session.publish({"method": "item/agentMessage/delta", "params": {"delta": "x"}})
        event = queue.get_nowait()
        assert event["method"] == "deepqueue/resync"
        assert event["params"]["pending"][0]["id"] == "req"


async def test_http_server_isolation_configuration_and_interruption(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (_, fixture):
        other = CodexFixture("other")
        path = str(tmp_path / "other.sock")
        db.add_server(Server(name="other", codex=CodexConnection(enabled=True, socket=path)))
        app = create_app(db.home)
        async with unix_serve(other.handle, path, compression=None):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
                headers={"X-DeepQueue": "1"},
            ) as client:
                base = "/api/servers/local/codex"
                assert (await client.get(base + "/threads")).status_code == 409
                assert (await client.post(base + "/connect", json={})).status_code == 200
                await client.post("/api/servers/other/codex/connect", json={})
                rows = (await client.get(base + "/threads")).json()["data"]
                assert [r["id"] for r in rows] == ["fixture-task"]
                assert (await client.get(base + "/threads/other-task")).status_code == 502
                assert not any(c.get("method") == "thread/read" for c in other.calls)
                created = (
                    await client.post(
                        base + "/threads", json={"cwd": "/tmp/project", "model": "gpt-5.6-luna"}
                    )
                ).json()
                tid = created["thread"]["id"]
                sent = (
                    await client.post(
                        f"{base}/threads/{tid}/messages",
                        json={"text": "等待", "request_id": "wait"},
                    )
                ).json()
                await client.post(
                    f"{base}/threads/{tid}/interrupt", json={"turn_id": sent["turn"]["id"]}
                )
                result = (await client.get(f"{base}/threads/{tid}")).json()["thread"]
                assert result["turns"][0]["status"] == "interrupted"
                assert (
                    await client.post(base + "/settings", json={"socket": "relative"})
                ).status_code == 422
                await client.post(base + "/disconnect", json={})
                assert fixture.threads[tid]["turns"]
            await app.state.codex.close()
            await other.close()


def test_gateway_requires_admin_and_same_origin_including_event_stream(db):
    access = Access(db.home)
    admin = access.create("Admin")
    scoped = access.create("Submit only", server="local")
    base = "/api/servers/local/codex"
    with TestClient(create_app(db.home), base_url="http://localhost") as client:
        for path in (base, base + "/threads", base + "/projects", base + "/events"):
            assert client.get(path).status_code == 401
            assert (
                client.get(path, headers={"Authorization": "Bearer " + scoped["token"]}).status_code
                == 403
            )
        headers = {"Authorization": "Bearer " + admin["token"], "X-DeepQueue": "1"}
        assert client.get(base, headers=headers).status_code == 200
        assert (
            client.post(
                base + "/connect", json={}, headers={**headers, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                base + "/connect", json={}, headers={"Authorization": headers["Authorization"]}
            ).status_code
            == 403
        )


async def test_projects_are_server_owned_paginated_and_used_when_creating_tasks(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        for index in range(102):
            fixture.add_project(
                f"project-{index}", f"Project {index}", [f"/remote/project-{index}"]
            )
        session = await gateway.session("local")
        await session.connect()
        first = await session.projects()
        assert first["supported"]
        assert len(first["data"]) == 100
        last = await session.projects(first["nextCursor"])
        assert [project["id"] for project in last["data"]] == ["project-100", "project-101"]
        assert not last["nextCursor"]
        result = await session.new_thread(
            NewThread(cwd="/remote/project-101/subdir", project_id="project-101")
        )
        assert result["thread"]["projectId"] == "project-101"
        assert fixture.calls[-1]["params"]["projectId"] == "project-101"
        standalone = await session.new_thread(NewThread(cwd="/remote/project-101"))
        assert standalone["thread"]["projectId"] is None
        assert session.status()["runtime"]["codexHome"] == "/tmp/codex-fixture-home/.codex"


async def test_old_runtime_without_projects_keeps_history_available(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        fixture.projects_supported = False
        session = await gateway.session("local")
        await session.connect()
        assert await session.projects() == {"data": [], "nextCursor": None, "supported": False}
        assert (await session.read_thread("fixture-task", 40))["thread"]["id"] == "fixture-task"


@pytest.mark.parametrize("authentication", ["password", "key"])
async def test_actual_ssh_websocket_proxy_with_saved_credentials(
    db, tmp_path, monkeypatch, authentication
):
    fixture = CodexFixture("remote")
    path = str(tmp_path / "remote-codex.sock")
    commands = []

    def proxy(self, channel, command):
        argv = shlex.split(command.decode())
        commands.append(argv)
        assert argv[-5:] == ["/opt/Codex bin/codex", "app-server", "proxy", "--sock", path]
        upstream = socket.socket(socket.AF_UNIX)
        upstream.connect(path)

        def upload():
            try:
                while data := channel.recv(65536):
                    upstream.sendall(data)
            except OSError:
                pass
            finally:
                upstream.close()

        def download():
            try:
                while data := upstream.recv(65536):
                    channel.sendall(data)
            except OSError:
                pass
            finally:
                channel.close()

        threading.Thread(target=upload, daemon=True).start()
        threading.Thread(target=download, daemon=True).start()
        return True

    monkeypatch.setattr(Auth, "check_channel_exec_request", proxy)
    async with unix_serve(fixture.handle, path, compression=None):
        with loopback_ssh(tmp_path) as ssh:
            trust_host(db.home, "127.0.0.1", ssh["port"], ssh["fingerprint"])
            vault = Secrets(db.home)
            auth = (
                {"password_ref": vault.put("fixture-password")}
                if authentication == "password"
                else {
                    "key_file": str(ssh["key"]),
                    "passphrase_ref": vault.put("fixture-passphrase"),
                }
            )
            server = Server(
                name="remote",
                kind="ssh",
                host="127.0.0.1",
                port=ssh["port"],
                username="fixture",
                codex=CodexConnection(enabled=True, socket=path, executable="/opt/Codex bin/codex"),
                **auth,
            )
            async with AppServer(
                Settings(), tmp_path / "trace", home=db.home, server=server
            ) as agent:
                rows = await agent.request("thread/list", {"limit": 30})
                assert rows["data"][0]["id"] == "remote-task"
            assert commands
        await fixture.close()


def test_cli_configure_and_remote_control_contract(db, tmp_path, monkeypatch):
    config = tmp_path / "codex.json"
    config.write_text(
        json.dumps({"enabled": True, "socket": "/tmp/shared.sock", "cwd": "/project"})
    )
    result = execute(
        parser().parse_args(
            ["--home", str(db.home), "codex", "configure", "local", "--file", str(config)]
        )
    )
    assert result["socket"] == "/tmp/shared.sock"
    calls = []
    monkeypatch.setattr(
        "deepqueue.remote.Client.request",
        lambda self, path, data=None, **kw: calls.append((path, data, kw)) or {},
    )
    monkeypatch.setenv("DEEPQUEUE_CLIENT_CONFIG", str(tmp_path / "empty.json"))
    args = parser().parse_args(
        [
            "--url",
            "https://queue.example",
            "codex",
            "send",
            "local",
            "task-1",
            "--text",
            "review",
            "--model",
            "gpt-5.6-luna",
            "--effort",
            "high",
            "--request-id",
            "stable",
        ]
    )
    execute(args)
    assert calls[0][0] == "/servers/local/codex/threads/task-1/messages"
    assert calls[0][1]["request_id"] == "stable"
    assert calls[0][1]["model"] == "gpt-5.6-luna"


async def test_queue_archive_uses_configured_server_codex_and_source_model(db, tmp_path):
    from test_return import SOURCE, ConversationServer, finish_job, source_job

    from deepqueue.scheduler import Scheduler

    backend = ConversationServer()
    async with backend.serve(tmp_path) as settings:
        db.set_server_codex("local", CodexConnection(enabled=True, socket=settings.agent_socket))
        settings.return_agent_socket = str(tmp_path / "wrong-global.sock")
        job = finish_job(db, source_job(db))
        scheduler = Scheduler(db.home, settings)
        agent_id = db.start_agent(job["id"], "archive", "source-thread")
        await scheduler.analyze(job, "archive", agent_id)
    detail = db.detail(job["id"])
    assert detail["archive_status"] == "completed"
    assert detail["agents"][-1]["thread_id"] == SOURCE
    assert detail["agents"][-1]["model"] == backend.model


async def test_reply_cannot_authorize_request_from_an_old_connection(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, _):
        session = await gateway.session("local")
        await session.connect()
        old_generation = session.generation
        await session.disconnect()
        await session.connect()
        # Remote runtime may reuse an ID; an old browser button still cannot answer it.
        session.pending['"reused"'] = {
            "id": "reused",
            "method": "item/commandExecution/requestApproval",
            "params": {},
        }
        with pytest.raises(HTTPException, match="连接已改变"):
            await session.reply(
                Reply(request_id="reused", generation=old_generation, result={"decision": "accept"})
            )
        assert session.pending


async def test_fresh_thread_uses_legacy_history_and_waits_for_materialization(db, tmp_path):
    from deepqueue.gateway import NewThread

    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        result = await session.new_thread(NewThread(cwd="/tmp/new-project", model="gpt-5.6-luna"))
        tid = result["thread"]["id"]
        opened = await session.open_thread(tid, 40)
        assert opened["thread"]["turns"] == []
        assert opened["thread"]["model"] == "gpt-5.6-luna"
        assert not any(c.get("method") in ("thread/resume", "thread/read") for c in fixture.calls)
        start = next(c for c in fixture.calls if c.get("method") == "thread/start")
        assert start["params"]["historyMode"] == "legacy"
        await session.send_message(tid, Message(text="first", request_id="first"))
        assert not any(c.get("method") == "thread/resume" for c in fixture.calls)
        assert (await session.read_thread(tid, 40))["thread"]["turns"]
