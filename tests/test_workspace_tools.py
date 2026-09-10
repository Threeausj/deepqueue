import asyncio
import base64
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_gateway import fixture_gateway, notification
from test_ssh import loopback_ssh

from deepqueue.gateway import Message
from deepqueue.history_cache import HistoryCache
from deepqueue.workspace import (
    MAX_FILE,
    ExtensionChoice,
    FileAccess,
    TerminalInput,
    TerminalRecovery,
    TerminalResize,
    TerminalSize,
)


def calls(fixture, method):
    return [c["params"] for c in fixture.calls if c.get("method") == method]


def test_history_cache_is_bounded_expiring_and_copies(monkeypatch):
    now = 1.0
    monkeypatch.setattr("deepqueue.history_cache.time.monotonic", lambda: now)
    cache = HistoryCache(entries=2, max_bytes=100, ttl=5)
    source = {"turns": [1]}
    cache.put("a", source)
    source["turns"].append(2)
    cache.get("a")["turns"].append(3)
    assert cache.get("a") == {"turns": [1]}
    cache.put("b", {})
    cache.get("a")
    cache.put("c", {})
    assert cache.get("b") is None
    cache.put("large", {"text": "x" * 101})
    assert cache.get("large") is None
    now = 6
    assert cache.get("a") is None
    cache.clear()
    assert cache.size == 0 and not cache.rows


async def test_cache_coalesces_resume_reads_and_invalidates_on_events(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        fixture.threads["fixture-task"]["turns"] = [
            {"id": str(i), "status": "completed", "items": []} for i in range(10)
        ]
        small, full = await asyncio.gather(
            session.open_thread("fixture-task", 1), session.open_thread("fixture-task", 40)
        )
        assert len(small["thread"]["turns"]) == 1
        assert len(full["thread"]["turns"]) == 10
        assert len(calls(fixture, "thread/read")) == len(calls(fixture, "thread/resume")) == 1
        await session.read_thread("fixture-task", 40, force=True)
        assert len(calls(fixture, "thread/read")) == 2
        events = asyncio.Queue()
        session.subscribers.add(events)
        fixture.threads["fixture-task"]["name"] = "Externally renamed"
        await fixture.emit(
            "thread/name/updated", {"threadId": "fixture-task", "threadName": "Externally renamed"}
        )
        await notification(events, "thread/name/updated")
        assert (await session.open_thread("fixture-task", 40))["thread"][
            "name"
        ] == "Externally renamed"
        assert len(calls(fixture, "thread/read")) == 3
        await session.disconnect()
        await session.connect()
        await session.open_thread("fixture-task", 40)
        assert len(calls(fixture, "thread/read")) == 4
        assert len(calls(fixture, "thread/resume")) == 2


async def test_inflight_cache_survives_cancel_without_retaining_stale_reads(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        original = session.rpc

        async def delayed(method, params):
            result = deepcopy(await original(method, params))
            if method == "thread/read":
                entered.set()
                await release.wait()
            return result

        session.rpc = delayed
        first = asyncio.create_task(session.read_thread("fixture-task", 40))
        await entered.wait()
        second = asyncio.create_task(session.read_thread("fixture-task", 40))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        fixture.threads["fixture-task"]["name"] = "New snapshot"
        session.invalidate_history("fixture-task")
        release.set()
        await second
        assert session.history.get("fixture-task") is None
        assert not session.history_pending
        assert len(calls(fixture, "thread/read")) == 1
        assert (await session.read_thread("fixture-task", 40))["thread"]["name"] == "New snapshot"


async def test_slash_catalog_native_inputs_validation_and_refresh(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        fixture.skills = [
            {"name": "experiment", "path": "/skills/experiment/SKILL.md", "enabled": True},
            {
                "name": "plot",
                "path": "/plugins/lab/plot/SKILL.md",
                "enabled": True,
                "pluginId": "lab@test",
            },
            {"name": "hidden", "path": "/skills/hidden/SKILL.md", "enabled": False},
        ]
        fixture.plugins = [
            {"id": "lab@test", "name": "lab", "installed": True, "enabled": True},
            {"id": "disabled@test", "name": "disabled", "installed": True, "enabled": False},
        ]
        catalog = await session.tools.extensions("fixture-task")
        await session.tools.extensions("fixture-task")
        assert len(catalog["skills"]) == 2 and len(catalog["plugins"]) == 1
        assert len(calls(fixture, "skills/list")) == 1
        assert calls(fixture, "skills/list")[0]["cwds"] == ["/tmp/codex-fixture"]
        message = Message(
            text="$experiment $lab 查看效果",
            request_id="extensions",
            extensions=[
                ExtensionChoice(kind="skill", id="/skills/experiment/SKILL.md"),
                ExtensionChoice(kind="plugin", id="lab@test"),
            ],
        )
        async with asyncio.timeout(5):  # Submission already holds the thread lock.
            await session.send_message("fixture-task", message)
        assert calls(fixture, "turn/start")[0]["input"][1:] == [
            {"type": "skill", "name": "experiment", "path": "/skills/experiment/SKILL.md"},
            {"type": "skill", "name": "plot", "path": "/plugins/lab/plot/SKILL.md"},
        ]
        with pytest.raises(HTTPException, match="变化"):
            await session.tools.inputs(
                "fixture-task", [ExtensionChoice(kind="skill", id="/etc/passwd")]
            )
        fixture.skills = []
        assert not (await session.tools.extensions("fixture-task", force=True))["skills"]
        assert calls(fixture, "skills/list")[-1]["forceReload"] is True


async def test_terminal_routes_use_native_pty_and_track_input_resize_exit(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        events = asyncio.Queue()
        session.subscribers.add(events)
        terminal, duplicate = await asyncio.gather(
            session.tools.start_terminal("fixture-task", TerminalSize(cols=120, rows=30)),
            session.tools.start_terminal("fixture-task", TerminalSize()),
        )
        assert terminal["id"] == duplicate["id"]
        await notification(events, "deepqueue/terminal/output")
        params = calls(fixture, "command/exec")[0]
        assert params["cwd"] == "/tmp/codex-fixture"
        assert params["size"] == {"cols": 120, "rows": 30}
        assert params["sandboxPolicy"]["type"] == "workspaceWrite"
        assert params["tty"] and params["disableTimeout"] and params["disableOutputCap"]
        assert len(calls(fixture, "command/exec")) == 1
        await session.tools.write_terminal(
            "fixture-task", TerminalInput(id=terminal["id"], data="精度: 0.93\r")
        )
        await notification(events, "deepqueue/terminal/output")
        status = session.tools.terminal_status("fixture-task")
        assert base64.b64decode(status["output_base64"]).endswith("精度: 0.93\r".encode())
        await session.tools.resize_terminal(
            "fixture-task", TerminalResize(id=terminal["id"], cols=80, rows=20)
        )
        assert calls(fixture, "command/exec/resize")[-1]["size"] == {"cols": 80, "rows": 20}
        with pytest.raises(HTTPException):
            await session.tools.write_terminal(
                "another-task", TerminalInput(id=terminal["id"], data="x")
            )
        await session.tools.stop_terminal("fixture-task", terminal["id"])
        await session.tools.terminals["fixture-task"]["task"]
        assert session.tools.terminal_status("fixture-task")["state"] == "exited"
        await session.tools.start_terminal("fixture-task", TerminalSize())
        await notification(events, "deepqueue/terminal/output")
        await session.disconnect()
        assert not fixture.terminals
        assert not session.tools.terminals


def test_file_access_reads_project_files_and_rejects_escape_and_special_files(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "模型.md").write_text("# 验证精度\n\n0.93")
    (project / "nested").mkdir()
    (tmp_path / "secret").write_text("outside")
    (project / "escape").symlink_to(tmp_path / "secret")
    (project / "inside").symlink_to(project / "模型.md")
    with (project / "large").open("wb") as f:
        f.truncate(MAX_FILE + 1)
    os.mkfifo(project / "pipe")
    files = FileAccess(SimpleNamespace(server=SimpleNamespace(kind="local")))
    listing = files.read(str(project), "", True)
    assert listing["entries"][0]["name"] == "nested"
    assert files.read(str(project), "模型.md")["text"] == "# 验证精度\n\n0.93"
    assert files.read(str(project), "inside")["text"].startswith("# 验证")
    for path, code in [
        ("../secret", 403),
        ("/etc/passwd", 403),
        ("escape", 403),
        ("large", 413),
        ("pipe", 400),
        ("absent", 404),
    ]:
        with pytest.raises(HTTPException) as error:
            files.read(str(project), path)
        assert error.value.status_code == code


@pytest.fixture
def ssh_server(tmp_path):
    with loopback_ssh(tmp_path) as server:
        yield server


def test_ssh_project_file_listing_and_utf8_preview(db, ssh_server, monkeypatch):
    import paramiko
    from test_ssh import Files

    from deepqueue.config import Secrets
    from deepqueue.models import Server
    from deepqueue.transport import trust_host

    def listing(self, path):
        result = []
        for child in self.path(path).iterdir():
            entry = paramiko.SFTPAttributes.from_stat(child.lstat())
            entry.filename = child.name
            result.append(entry)
        return result

    monkeypatch.setattr(Files, "list_folder", listing)
    root = ssh_server["root"]
    project = root / "project"
    project.mkdir()
    (project / "metrics.md").write_text("# 远端精度\n0.93")
    trust_host(db.home, "127.0.0.1", ssh_server["port"], ssh_server["fingerprint"])
    server = Server(
        name="ssh-files",
        kind="ssh",
        host="127.0.0.1",
        port=ssh_server["port"],
        username="fixture",
        password_ref=Secrets(db.home).put("fixture-password"),
    )
    access = FileAccess(SimpleNamespace(home=db.home, server=server))
    try:
        assert access.read(str(project), "", True)["entries"][0]["name"] == "metrics.md"
        assert access.read(str(project), "metrics.md")["text"] == "# 远端精度\n0.93"
    finally:
        access.close()


async def test_namespace_failure_requires_explicit_single_terminal_recovery(db, tmp_path):
    from pydantic import ValidationError

    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        fixture.namespace_failure = True
        session = await gateway.session("local")
        await session.connect()
        before = deepcopy(fixture.threads["fixture-task"])
        failed = await session.tools.start_terminal("fixture-task", TerminalSize())
        await session.tools.terminals["fixture-task"]["task"]
        state = session.tools.terminal_status("fixture-task")
        assert state["state"] == "exited" and state["recovery_available"]
        assert "用户命名空间" in state["error"]
        assert len(calls(fixture, "command/exec")) == 1  # No automatic escalation.
        with pytest.raises(ValidationError):
            TerminalRecovery(id=failed["id"])
        with pytest.raises(ValidationError):
            TerminalRecovery(id=failed["id"], allow_server_access=False)
        stale = TerminalRecovery(id="stale-terminal", allow_server_access=True)
        with pytest.raises(HTTPException, match="已变化"):
            await session.tools.start_terminal("fixture-task", stale, recovery=stale)
        choice = TerminalRecovery(id=failed["id"], allow_server_access=True)
        events = asyncio.Queue()
        session.subscribers.add(events)
        recovered = await session.tools.start_terminal("fixture-task", choice, recovery=choice)
        await notification(events, "deepqueue/terminal/output")
        assert recovered["execution_mode"] == "server"
        assert calls(fixture, "command/exec")[-1]["sandboxPolicy"] == {"type": "dangerFullAccess"}
        assert fixture.threads["fixture-task"] == before
        assert not calls(fixture, "thread/settings/update")
        assert not calls(fixture, "turn/start")
        with pytest.raises(HTTPException):
            await session.tools.start_terminal("fixture-task", choice, recovery=choice)
        await session.tools.stop_terminal("fixture-task", recovered["id"])
        await session.tools.terminals["fixture-task"]["task"]
        await session.tools.start_terminal("fixture-task", TerminalSize())
        await session.tools.terminals["fixture-task"]["task"]
        assert calls(fixture, "command/exec")[-1]["sandboxPolicy"] == before["sandbox"]
        assert fixture.threads["fixture-task"] == before
