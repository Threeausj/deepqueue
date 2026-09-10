"""Per-server Codex sessions. Codex remains the authority for task history and execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from .access import COOKIE, Access
from .agent import AppServer, AppServerError
from .codex_transport import start_daemon
from .config import load_settings
from .history_cache import HistoryCache
from .models import CodexConnection, EffortId, Model, ModelId, Server
from .preview import PreviewManager, PreviewReference, PreviewTarget, credential
from .workspace import ExtensionChoice, TerminalInput, TerminalResize, TerminalSize, WorkspaceTools

SUPPORTED_REQUESTS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
}


class Connect(Model):
    start: bool = False


class AccessOptions(Model):
    permissions: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f]+$"
    )
    approval_policy: Literal["untrusted", "on-request", "never"] | None = None

    def access_params(self):
        return {
            key: value
            for key, value in {
                "permissions": self.permissions,
                "approvalPolicy": self.approval_policy,
            }.items()
            if value is not None
        }


class NewThread(AccessOptions):
    cwd: str = Field(min_length=1, max_length=4096, pattern=r"^/[^\x00]*$")
    model: ModelId | None = None
    project_id: str | None = Field(default=None, min_length=1, max_length=256)


class Message(AccessOptions):
    extensions: list[ExtensionChoice] = Field(default_factory=list, max_length=16)
    text: str = Field(min_length=1, max_length=128000)
    model: ModelId | None = None
    effort: EffortId | None = None
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class ForkThread(Model):
    last_turn_id: str | None = Field(default=None, min_length=1, max_length=128)
    model: ModelId | None = None
    effort: EffortId | None = None
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class RenameThread(Model):
    name: str = Field(min_length=1, max_length=200)


class Interrupt(Model):
    turn_id: str = Field(min_length=1, max_length=128)


class Reply(Model):
    request_id: str | int
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")
    result: dict


def request_key(value):
    return json.dumps(value)


def limited_history(thread, limit):
    """Bound browser payloads only; never alter Codex or archival evidence."""
    thread = dict(thread)
    turns = thread.get("turns", [])
    thread["omittedTurns"] = max(0, len(turns) - limit)
    thread["turns"] = turns[-limit:]
    return thread


class Session:
    def __init__(self, home, server, factory):
        self.home, self.server, self.factory = home, server, factory
        self.lock = asyncio.Lock()
        self.thread_locks = {}
        self.client = None
        self.reader = None
        self.directory = None
        self.state = "disconnected"
        self.error = None
        self.pending = {}
        self.subscribers = set()
        self.sent = OrderedDict()
        self.forks = OrderedDict()
        self.thread_settings = {}
        self.settings_waiters = {}
        self.fresh_threads = {}
        self.generation = None
        self.history = HistoryCache()
        self.history_pending = {}
        self.history_versions = {}
        self.loaded_threads = set()
        self.tools = WorkspaceTools(self)

    def status(self):
        return {
            "server": self.server.name,
            "state": self.state,
            "error": self.error,
            "config": self.server.codex.model_dump(),
            "generation": self.generation,
            "runtime": self.client.initialized if self.client else None,
            "pending": list(self.pending.values()),
        }

    def publish(self, message):
        for queue in self.subscribers:
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"method": "deepqueue/resync", "params": self.status()})
            else:
                queue.put_nowait(message)

    async def _close(self):
        await self.tools.close()
        self.history.clear()
        self.loaded_threads.clear()
        for task in self.history_pending.values():
            task.cancel()
        await asyncio.gather(*self.history_pending.values(), return_exceptions=True)
        self.history_pending.clear()
        self.history_versions.clear()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.client:
            await self.client.__aexit__(None, None, None)
            self.client = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None
        self.pending.clear()
        self.fresh_threads.clear()
        self.thread_settings.clear()
        for future in self.settings_waiters.values():
            if not future.done():
                future.set_exception(HTTPException(409, "连接已断开，请重新读取任务权限"))
        self.settings_waiters.clear()

    async def disconnect(self):
        async with self.lock:
            await self._close()
            self.state, self.error = "disconnected", None
            self.publish({"method": "deepqueue/status", "params": self.status()})

    async def connect(self, start=False):
        async with self.lock:
            if not self.server.codex.enabled:
                raise ValueError("请先保存并启用此服务器的 Codex 连接")
            if self.state == "connected":
                return self.status()
            await self._close()
            self.state, self.error = "connecting", None
            self.publish({"method": "deepqueue/status", "params": self.status()})
            try:
                if start:
                    await asyncio.to_thread(start_daemon, self.home, self.server)
                self.directory = tempfile.TemporaryDirectory(prefix="deepqueue-codex-")
                self.client = self.factory(
                    load_settings(self.home),
                    Path(self.directory.name),
                    home=self.home,
                    server=self.server,
                    interactive=True,
                )
                await self.client.__aenter__()
                self.generation = uuid.uuid4().hex
                self.state = "connected"
                self.reader = asyncio.create_task(self._listen())
            except BaseException as exc:
                detail = str(exc) or type(exc).__name__
                if self.directory:
                    error_log = Path(self.directory.name) / "stderr.log"
                    if error_log.exists():
                        with error_log.open("rb") as file:
                            file.seek(max(0, error_log.stat().st_size - 3000))
                            detail += " " + file.read().decode("utf-8", "replace").strip()
                await self._close()
                self.state, self.error = "error", detail
                self.publish({"method": "deepqueue/status", "params": self.status()})
                raise
            self.publish({"method": "deepqueue/status", "params": self.status()})
            return self.status()

    async def _listen(self):
        try:
            await self._listen_events()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state, self.error = "error", str(exc) or type(exc).__name__
            self.pending.clear()
            self.publish({"method": "deepqueue/status", "params": self.status()})

    async def _listen_events(self):
        while True:
            message = await self.client.notifications.get()
            method, params = message.get("method"), message.get("params", {})
            self.tools.event(method, params)
            thread_id = params.get("threadId") or params.get("thread", {}).get("id")
            if thread_id:
                self.invalidate_history(thread_id)
                if method in ("thread/closed", "thread/unloaded", "thread/archived"):
                    self.loaded_threads.discard(thread_id)
            if "id" in message:
                if method == "currentTime/read":
                    await self.client.send(
                        {"id": message["id"], "result": {"currentTimeAt": int(time.time())}}
                    )
                    continue
                if method not in SUPPORTED_REQUESTS or len(self.pending) >= 100:
                    await self.client.send(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "Request not supported by DeepQueue web client",
                            },
                        }
                    )
                    self.publish(
                        {
                            "method": "deepqueue/unsupportedRequest",
                            "params": {
                                "threadId": params.get("threadId"),
                                "method": method,
                            },
                        }
                    )
                    continue
                self.pending[request_key(message["id"])] = message
            elif method == "serverRequest/resolved":
                self.pending.pop(request_key(params.get("requestId")), None)
            elif method == "turn/completed":
                self.pending = {
                    key: value
                    for key, value in self.pending.items()
                    if value.get("params", {}).get("turnId") != params.get("turn", {}).get("id")
                }
            elif method == "turn/started":
                self.fresh_threads.pop(params.get("threadId"), None)
            elif method == "thread/settings/updated":
                self.remember_settings(params["threadId"], params["threadSettings"])
                future = self.settings_waiters.get(params["threadId"])
                if future and not future.done():
                    future.set_result(None)
            elif method == "deepqueue/disconnected":
                self.state, self.error = "error", message.get("error", "连接已断开")
                self.pending.clear()
                self.publish({"method": "deepqueue/status", "params": self.status()})
                return
            self.publish(message)

    def require_client(self):
        if self.state != "connected" or not self.client:
            raise HTTPException(409, "Codex 尚未连接，请连接此服务器后重试")
        return self.client

    async def rpc(self, method, params):
        client = self.require_client()
        try:
            return await client.request(method, params)
        except Exception as exc:
            if "list_turns is not supported yet" in str(exc):
                raise HTTPException(
                    502,
                    "此服务器的 Codex 尚不支持读取该任务的历史格式。"
                    "请在原 Codex 客户端打开；网页新建任务使用兼容格式。",
                ) from exc
            raise HTTPException(502, str(exc) or "Codex 响应超时；请刷新任务确认操作结果") from exc

    async def projects(self, cursor=None):
        client = self.require_client()
        try:
            result = await client.request(
                "project/list", {"limit": 100, "cursor": cursor, "sortKey": "position"}
            )
        except AppServerError as exc:
            # Older runtimes have directory-based history but no project catalog.
            try:
                error = json.loads(str(exc))
            except ValueError:
                error = {}
            unsupported = error.get("code") == -32601 or (
                error.get("code") == -32600
                and "unknown variant" in error.get("message", "")
                and "project/list" in error.get("message", "")
            )
            if not unsupported:
                raise HTTPException(502, str(exc)) from exc
            return {"data": [], "nextCursor": None, "supported": False}
        except Exception as exc:
            raise HTTPException(502, str(exc) or "读取 Codex 项目失败") from exc
        return {**result, "supported": True}

    async def new_thread(self, data):
        # Explicit legacy history remains resumable by queue callbacks on shared runtimes.
        params = {"cwd": data.cwd, "historyMode": "legacy", **data.access_params()}
        if data.model:
            params["model"] = data.model
        if data.project_id:
            params["projectId"] = data.project_id
        result = await self.rpc("thread/start", params)
        thread_id = result["thread"]["id"]
        self.fresh_threads[thread_id] = result["thread"]
        self.remember_settings(thread_id, result)
        return result

    def remember_settings(self, thread_id, result):
        settings = self.thread_settings.setdefault(thread_id, {})
        for field in (
            "model",
            "reasoningEffort",
            "approvalPolicy",
            "approvalsReviewer",
            "sandbox",
            "activePermissionProfile",
            "cwd",
        ):
            if field in result:
                settings[field] = result[field]
        for source, target in (("effort", "reasoningEffort"), ("sandboxPolicy", "sandbox")):
            if source in result:
                settings[target] = result[source]

    async def permissions(self, cwd=None):
        profiles, cursor = [], None
        for _ in range(100):
            result = await self.rpc(
                "permissionProfile/list", {"cwd": cwd, "limit": 100, "cursor": cursor}
            )
            profiles.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                requirements = await self.rpc("configRequirements/read", {})
                return {"data": profiles, "requirements": requirements.get("requirements")}
        raise HTTPException(502, "权限配置过多，未能完整读取")

    async def update_access(self, thread_id, data):
        params = data.access_params()
        if not params:
            raise HTTPException(422, "请指定权限配置或审批策略")
        async with self.thread_locks.setdefault(thread_id, asyncio.Lock()):
            await self.resume_thread(thread_id)
            await self.set_thread_settings(thread_id, params)
            # Read back Codex's effective settings, including administrator restrictions.
            # Empty legacy tasks are live but have no resumable rollout yet.
            if thread_id not in self.fresh_threads:
                await self.resume_thread(thread_id)
            return await self.read_thread(thread_id, 40)

    async def set_thread_settings(self, thread_id, params):
        current = self.thread_settings.get(thread_id, {})
        comparable = {
            **current,
            "permissions": (current.get("activePermissionProfile") or {}).get("id"),
            "sandboxPolicy": current.get("sandbox"),
        }
        if all(key in comparable and comparable[key] == value for key, value in params.items()):
            # Codex deliberately emits no settings notification for an unchanged update.
            return
        # The acknowledgement is empty; the native notification carries resolved permissions.
        # It can arrive after the RPC response, especially for an unmaterialized new task.
        future = asyncio.get_running_loop().create_future()
        self.settings_waiters[thread_id] = future
        try:
            await self.rpc("thread/settings/update", {"threadId": thread_id, **params})
            await asyncio.wait_for(future, 5)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                502, "权限设置已提交，但尚未收到生效状态，请重新打开任务核对"
            ) from exc
        finally:
            self.settings_waiters.pop(thread_id, None)
            if not future.done():
                future.cancel()

    async def rename_thread(self, thread_id, data):
        name = data.name.strip()
        if not name:
            raise HTTPException(422, "任务名称不能为空")
        async with self.thread_locks.setdefault(thread_id, asyncio.Lock()):
            await self.resume_thread(thread_id)
            await self.rpc("thread/name/set", {"threadId": thread_id, "name": name})
            self.invalidate_history(thread_id)
            if thread_id in self.fresh_threads:
                self.fresh_threads[thread_id]["name"] = name
            return {"ok": True, "name": name}

    async def fork_thread(self, thread_id, data):
        key = (thread_id, data.request_id)
        async with self.thread_locks.setdefault(thread_id, asyncio.Lock()):
            if key in self.forks:
                old, result = self.forks[key]
                if old != data.model_dump():
                    raise HTTPException(409, "request_id 已用于另一处分支")
                if result is None:
                    raise HTTPException(409, "分支结果待确认，请刷新任务目录，避免重复创建")
                return result
            source = (await self.read_thread(thread_id, 1, force=True))["thread"]
            if not source.get("turns"):
                raise HTTPException(409, "请先完成一轮对话再创建分支")
            if not data.last_turn_id and source["turns"][-1]["status"] == "inProgress":
                raise HTTPException(409, "当前轮次尚未结束，可从之前已完成的回复创建分支")
            resumed = await self.resume_thread(thread_id)
            params = {
                "threadId": thread_id,
                "excludeTurns": True,
                # A copied goal must not start autonomous work before the user sends a message.
                "deferGoalContinuation": True,
                "model": data.model or resumed.get("model"),
                "cwd": resumed.get("cwd") or source.get("cwd"),
            }
            if data.last_turn_id:
                params["lastTurnId"] = data.last_turn_id
            effort = data.effort or resumed.get("reasoningEffort")
            if effort:
                params["config"] = {"model_reasoning_effort": effort}
            for field in ("approvalPolicy", "approvalsReviewer"):
                if field in resumed:
                    params[field] = resumed[field]
            profile = (resumed.get("activePermissionProfile") or {}).get("id")
            if profile:
                params["permissions"] = profile
            else:
                # A custom sandbox needs the full policy below, not just its mode.
                params["sandbox"] = "read-only"
            self.forks[key] = (data.model_dump(), None)
            while len(self.forks) > 256:
                self.forks.popitem(last=False)
            try:
                result = await self.require_client().request("thread/fork", params)
            except AppServerError as exc:
                # A definitive RPC rejection is retryable; a lost response is not.
                self.forks.pop(key, None)
                raise HTTPException(502, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, f"分支结果待确认，请刷新任务目录：{exc}") from exc
            self.forks[key] = (data.model_dump(), result)
            fork = result["thread"]
            self.remember_settings(fork["id"], result)
            warnings = []
            if not profile and resumed.get("sandbox"):
                try:
                    await self.set_thread_settings(
                        fork["id"], {"sandboxPolicy": resumed["sandbox"]}
                    )
                    updated = await self.resume_thread(fork["id"])
                    result.update({k: v for k, v in updated.items() if k != "thread"})
                except HTTPException as exc:
                    warnings.append(f"分支已创建，权限同步未完成，请查看访问权限：{exc.detail}")
            if "projectId" in source and fork.get("projectId") != source["projectId"]:
                try:
                    await self.rpc(
                        "thread/metadata/update",
                        {
                            "threadId": fork["id"],
                            "projectId": source["projectId"] or "",
                        },
                    )
                    fork["projectId"] = source["projectId"]
                except HTTPException as exc:
                    warnings.append(f"分支已创建，项目归属同步失败：{exc.detail}")
            if warnings:
                result["warnings"] = warnings
            return result

    async def resume_thread(self, thread_id):
        self.require_client()
        if thread_id in self.fresh_threads:
            return {"thread": {"id": thread_id}, **self.thread_settings.get(thread_id, {})}
        resumed = await self.rpc("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        if resumed.get("thread", {}).get("id") != thread_id:
            raise HTTPException(502, "Codex 返回了另一个任务，请重新连接后重试")
        self.remember_settings(thread_id, resumed)
        self.loaded_threads.add(thread_id)
        return resumed

    async def open_thread(self, thread_id, limit, force=False):
        async with self.thread_locks.setdefault(thread_id, asyncio.Lock()):
            if force or thread_id not in self.loaded_threads:
                await self.resume_thread(thread_id)
            return await self.read_thread(thread_id, limit, force=force)

    def invalidate_history(self, thread_id):
        self.history.discard(thread_id)
        if thread_id in self.history_pending:
            self.history_versions[thread_id] = self.history_versions.get(thread_id, 0) + 1

    async def _fetch_history(self, thread_id):
        version = self.history_versions.get(thread_id, 0)
        result = await self.rpc("thread/read", {"threadId": thread_id, "includeTurns": True})
        if result.get("thread", {}).get("id") != thread_id:
            raise HTTPException(502, "Codex 返回了另一个任务，请重新连接后重试")
        if version == self.history_versions.get(thread_id, 0):
            self.history.put(thread_id, result)
        return result

    async def read_thread(self, thread_id, limit, force=False):
        self.require_client()
        if force:
            pending = self.history_pending.get(thread_id)
            if pending:
                with contextlib.suppress(Exception):
                    await asyncio.shield(pending)
            self.invalidate_history(thread_id)
        if thread_id in self.fresh_threads:
            # Codex materializes the rollout only after the first user message.
            result = {"thread": {**self.fresh_threads[thread_id], "turns": []}}
        else:
            result = self.history.get(thread_id)
            if result is None:
                task = self.history_pending.get(thread_id)
                if task is None:
                    task = asyncio.create_task(self._fetch_history(thread_id))
                    self.history_pending[thread_id] = task

                    def finished(done):
                        if not done.cancelled():
                            done.exception()  # Consume errors if every HTTP reader disconnected.
                        if self.history_pending.get(thread_id) is done:
                            self.history_pending.pop(thread_id, None)
                            self.history_versions.pop(thread_id, None)

                    task.add_done_callback(finished)
                from copy import deepcopy

                result = deepcopy(await asyncio.shield(task))
        if result.get("thread", {}).get("id") != thread_id:
            raise HTTPException(502, "Codex 返回了另一个任务，请重新连接后重试")
        result["thread"] = limited_history(
            {
                **result["thread"],
                **self.thread_settings.get(thread_id, {}),
            },
            limit,
        )
        return result

    async def send_message(self, thread_id, data):
        # Browser retries must not silently append a second user message. An uncertain send is
        # retained until reconnect too: it may already have been accepted remotely.
        key = (thread_id, data.request_id)
        async with self.thread_locks.setdefault(thread_id, asyncio.Lock()):
            if key in self.sent:
                old, result = self.sent[key]
                if old != data.model_dump():
                    raise HTTPException(409, "request_id 已用于另一条消息")
                if result is None:
                    raise HTTPException(409, "此消息的发送结果待确认，请刷新任务后查看")
                return result
            client = self.require_client()
            # Resume before sending so saved tasks work after either side reconnects.
            resumed = await self.resume_thread(thread_id)
            if resumed.get("thread", {}).get("id") != thread_id:
                raise HTTPException(502, "Codex 返回了另一个任务，消息未发送")
            extension_inputs = await self.tools.inputs(thread_id, data.extensions)
            self.sent[key] = (data.model_dump(), None)
            while len(self.sent) > 256:
                self.sent.popitem(last=False)
            params = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": data.text}, *extension_inputs],
                "clientUserMessageId": data.request_id,
                **data.access_params(),
            }
            for field in ("model", "effort"):
                if getattr(data, field):
                    params[field] = getattr(data, field)
            try:
                result = await client.request("turn/start", params)
            except Exception as exc:
                raise HTTPException(
                    502, f"消息结果待确认，请刷新任务，避免重复发送：{exc}"
                ) from exc
            self.sent[key] = (data.model_dump(), result)
            self.fresh_threads.pop(thread_id, None)
            self.invalidate_history(thread_id)
            self.thread_settings[thread_id].update(
                {
                    "model": data.model or resumed.get("model"),
                    "reasoningEffort": data.effort or resumed.get("reasoningEffort"),
                }
            )
            return result

    async def reply(self, data):
        async with self.lock:
            client = self.require_client()
            if data.generation != self.generation:
                raise HTTPException(409, "连接已改变，请刷新后处理当前请求")
            key = request_key(data.request_id)
            message = self.pending.get(key)
            if not message:
                raise HTTPException(409, "该请求已处理或连接已改变，请刷新任务")
            method, result = message["method"], data.result
            if method in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                decision = result.get("decision")
                allowed = message.get("params", {}).get("availableDecisions")
                if decision not in ("accept", "acceptForSession", "decline", "cancel") or (
                    allowed is not None and decision not in allowed
                ):
                    raise ValueError("Invalid approval decision")
                result = {"decision": decision}
            elif method == "item/tool/requestUserInput":
                questions = message.get("params", {}).get("questions", [])
                answers = result.get("answers", {})
                if set(answers) != {q["id"] for q in questions} or any(
                    not isinstance(answer, dict)
                    or not isinstance(answer.get("answers"), list)
                    or not answer["answers"]
                    or any(not isinstance(v, str) for v in answer["answers"])
                    for answer in answers.values()
                ):
                    raise ValueError("Each question requires a text answer")
                result = {"answers": answers}
            elif method == "item/permissions/requestApproval":
                requested = message.get("params", {}).get("permissions", {})
                if result not in (
                    {"permissions": {}, "scope": "turn"},
                    {"permissions": requested, "scope": "turn"},
                ):
                    raise ValueError("Only the displayed permissions may be granted")
            elif method == "mcpServer/elicitation/request":
                if result.get("action") not in ("decline", "cancel"):
                    raise ValueError("Handle this MCP request in Codex or decline it here")
                result = {"action": result["action"], "content": None}
            await client.send({"id": message["id"], "result": result})
            self.pending.pop(key, None)
            self.publish(
                {
                    "method": "serverRequest/resolved",
                    "params": {
                        "requestId": message["id"],
                        "threadId": message.get("params", {}).get("threadId"),
                    },
                }
            )
            return {"ok": True}


class CodexGateway:
    def __init__(self, home, db, factory=AppServer):
        self.home, self.db, self.factory = home, db, factory
        self.sessions = {}
        self.previews = PreviewManager(self)

    async def session(self, name):
        server = Server.model_validate(self.db.server(name)["config"])
        if name not in self.sessions:
            self.sessions[name] = Session(self.home, server, self.factory)
        session = self.sessions[name]
        connection_fields = {
            "host",
            "port",
            "username",
            "kind",
            "key_file",
            "password_ref",
            "passphrase_ref",
            "codex",
        }
        if server.model_dump(include=connection_fields) != session.server.model_dump(
            include=connection_fields
        ):
            await session.disconnect()
            session.server = server
            session.publish({"method": "deepqueue/status", "params": session.status()})
        return session

    async def close(self):
        await self.previews.close()
        await asyncio.gather(*(s.disconnect() for s in self.sessions.values()))


def router(gateway):
    routes = APIRouter(prefix="/api/servers/{name}/codex")

    @routes.get("")
    async def status(name: str):
        return (await gateway.session(name)).status()

    @routes.post("/settings")
    async def settings(name: str, data: CodexConnection):
        session = await gateway.session(name)
        await session.disconnect()
        gateway.db.set_server_codex(name, data)
        return (await gateway.session(name)).status()

    @routes.post("/connect")
    async def connect_server(name: str, data: Connect):
        try:
            return await (await gateway.session(name)).connect(data.start)
        except ValueError:
            raise
        except Exception as exc:
            session = await gateway.session(name)
            raise HTTPException(502, session.error or str(exc)) from exc

    @routes.post("/disconnect")
    async def disconnect_server(name: str):
        session = await gateway.session(name)
        await session.disconnect()
        return session.status()

    @routes.get("/models")
    async def models(name: str):
        session = await gateway.session(name)
        session.require_client()
        try:
            return {"data": await session.client.models()}
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @routes.get("/threads")
    async def threads(
        name: str,
        cursor: str | None = Query(None, max_length=4096),
        search: str = Query("", max_length=200),
    ):
        return await (await gateway.session(name)).rpc(
            "thread/list",
            {
                "limit": 100,
                "cursor": cursor,
                "sortKey": "updated_at",
                "searchTerm": search or None,
                "modelProviders": [],
                "sourceKinds": ["cli", "vscode", "appServer", "exec", "unknown"],
            },
        )

    @routes.get("/projects")
    async def projects(name: str, cursor: str | None = Query(None, max_length=4096)):
        return await (await gateway.session(name)).projects(cursor)

    @routes.get("/permissions")
    async def permissions(
        name: str, cwd: str | None = Query(None, max_length=4096, pattern=r"^/[^\x00]*$")
    ):
        return await (await gateway.session(name)).permissions(cwd)

    @routes.post("/threads")
    async def new_thread(name: str, data: NewThread):
        return await (await gateway.session(name)).new_thread(data)

    @routes.post("/threads/{thread_id}/open")
    async def open_thread(
        name: str, thread_id: str, limit: int = Query(40, ge=1, le=1000), force: bool = False
    ):
        return await (await gateway.session(name)).open_thread(thread_id, limit, force=force)

    @routes.get("/threads/{thread_id}")
    async def read_thread(
        name: str, thread_id: str, limit: int = Query(40, ge=1, le=1000), force: bool = False
    ):
        return await (await gateway.session(name)).read_thread(thread_id, limit, force=force)

    @routes.post("/threads/{thread_id}/preview")
    async def create_preview(name: str, thread_id: str, data: PreviewTarget, request: Request):
        return await gateway.previews.create(await gateway.session(name), thread_id, data, request)

    @routes.post("/threads/{thread_id}/preview/renew")
    async def renew_preview(name: str, thread_id: str, data: PreviewReference, request: Request):
        preview = gateway.previews.checked(data, await gateway.session(name), thread_id)
        preview.auth = credential(request)
        preview.expires = time.monotonic() + 1800
        return {"ok": True}

    @routes.post("/threads/{thread_id}/preview/close")
    async def close_preview(name: str, thread_id: str, data: PreviewReference):
        preview = gateway.previews.checked(data, await gateway.session(name), thread_id)
        gateway.previews.previews.pop(data.id, None)
        await preview.close()
        return {"ok": True}

    @routes.get("/threads/{thread_id}/extensions")
    async def extensions(name: str, thread_id: str, force: bool = False):
        return await (await gateway.session(name)).tools.extensions(thread_id, force)

    @routes.get("/threads/{thread_id}/files")
    async def files(name: str, thread_id: str, path: str = Query("", max_length=4096)):
        return await (await gateway.session(name)).tools.file(thread_id, path, directory=True)

    @routes.get("/threads/{thread_id}/file")
    async def file(name: str, thread_id: str, path: str = Query(..., max_length=4096)):
        return await (await gateway.session(name)).tools.file(thread_id, path)

    @routes.get("/threads/{thread_id}/terminal")
    async def terminal(name: str, thread_id: str):
        session = await gateway.session(name)
        session.require_client()
        return session.tools.terminal_status(thread_id)

    @routes.post("/threads/{thread_id}/terminal")
    async def start_terminal(name: str, thread_id: str, data: TerminalSize):
        return await (await gateway.session(name)).tools.start_terminal(thread_id, data)

    @routes.post("/threads/{thread_id}/terminal/input")
    async def terminal_input(name: str, thread_id: str, data: TerminalInput):
        return await (await gateway.session(name)).tools.write_terminal(thread_id, data)

    @routes.post("/threads/{thread_id}/terminal/resize")
    async def terminal_resize(name: str, thread_id: str, data: TerminalResize):
        return await (await gateway.session(name)).tools.resize_terminal(thread_id, data)

    @routes.post("/threads/{thread_id}/terminal/stop")
    async def terminal_stop(name: str, thread_id: str, data: TerminalInput):
        return await (await gateway.session(name)).tools.stop_terminal(thread_id, data.id)

    @routes.post("/threads/{thread_id}/messages")
    async def send_message(name: str, thread_id: str, data: Message):
        return await (await gateway.session(name)).send_message(thread_id, data)

    @routes.post("/threads/{thread_id}/access")
    async def update_access(name: str, thread_id: str, data: AccessOptions):
        return await (await gateway.session(name)).update_access(thread_id, data)

    @routes.post("/threads/{thread_id}/fork")
    async def fork_thread(name: str, thread_id: str, data: ForkThread):
        return await (await gateway.session(name)).fork_thread(thread_id, data)

    @routes.post("/threads/{thread_id}/name")
    async def rename_thread(name: str, thread_id: str, data: RenameThread):
        return await (await gateway.session(name)).rename_thread(thread_id, data)

    @routes.post("/threads/{thread_id}/interrupt")
    async def interrupt(name: str, thread_id: str, data: Interrupt):
        return await (await gateway.session(name)).rpc(
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": data.turn_id,
            },
        )

    @routes.post("/reply")
    async def reply(name: str, data: Reply):
        return await (await gateway.session(name)).reply(data)

    @routes.get("/events")
    async def events(name: str, request: Request):
        session = await gateway.session(name)
        access = Access(gateway.home)

        def authenticated():
            if not access.enabled() and not load_settings(gateway.home).public_url:
                return True
            authorization = request.headers.get("authorization", "")
            principal = (
                access.authenticate(authorization[7:])
                if authorization.startswith("Bearer ")
                else access.authenticate_session(request.cookies.get(COOKIE))
            )
            return principal is not None and principal["server"] is None

        async def stream():
            queue = asyncio.Queue(maxsize=256)
            session.subscribers.add(queue)
            try:
                yield (
                    "data: "
                    + json.dumps({"method": "deepqueue/status", "params": session.status()})
                    + "\n\n"
                )
                checked = time.monotonic()
                while not await request.is_disconnected():
                    if time.monotonic() - checked > 15:
                        if not authenticated():
                            yield 'data: {"method":"deepqueue/unauthorized"}\n\n'
                            return
                        checked = time.monotonic()
                    try:
                        message = await asyncio.wait_for(queue.get(), 15)
                        yield "data: " + json.dumps(message) + "\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                session.subscribers.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-store",
            },
        )

    return routes
