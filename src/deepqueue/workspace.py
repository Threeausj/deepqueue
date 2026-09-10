"""Interactive tools scoped to one server connection and a Codex task."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import mimetypes
import posixpath
import stat
import threading
import uuid
from pathlib import Path

from fastapi import HTTPException
from pydantic import Field

from .history_cache import HistoryCache
from .models import Model
from .transport import Transport

MAX_FILE = 8 * 1024 * 1024


class ExtensionChoice(Model):
    kind: str = Field(pattern=r"^(skill|plugin)$")
    id: str = Field(min_length=1, max_length=4096)


class TerminalSize(Model):
    cols: int = Field(default=100, ge=10, le=400)
    rows: int = Field(default=28, ge=3, le=150)


class TerminalInput(Model):
    id: str = Field(min_length=1, max_length=128)
    data: str = Field(max_length=16384)


class TerminalResize(TerminalSize):
    id: str = Field(min_length=1, max_length=128)


class FileAccess:
    def __init__(self, session):
        self.session = session
        self.lock = threading.RLock()
        self.context = None
        self.client = None
        self.sftp = None

    def close(self):
        with self.lock:
            if self.context:
                self.context.__exit__(None, None, None)
            self.context = self.client = self.sftp = None

    def remote(self):
        if self.client and not self.client.get_transport().is_active():
            self.close()
        if self.sftp is None:
            self.context = Transport(self.session.home, self.session.server).ssh()
            self.client = self.context.__enter__()
            self.client.get_transport().set_keepalive(15)
            self.sftp = self.client.open_sftp()
            self.sftp.get_channel().settimeout(15)
        return self.sftp

    def read(self, cwd, path, directory=False):
        if path.startswith("/") or "\x00" in path or ".." in path.split("/"):
            raise HTTPException(403, "只能查看当前项目内的文件")
        with self.lock:
            try:
                remote = self.remote() if self.session.server.kind == "ssh" else None
                root = remote.normalize(cwd) if remote else str(Path(cwd).resolve(strict=True))
                target = posixpath.join(root, path)
                target = (
                    remote.normalize(target) if remote else str(Path(target).resolve(strict=True))
                )
                if target != root and not target.startswith(root.rstrip("/") + "/"):
                    raise HTTPException(403, "此链接指向项目目录之外")
                info = remote.stat(target) if remote else Path(target).stat()
                if directory:
                    if not stat.S_ISDIR(info.st_mode):
                        raise HTTPException(400, "不是文件夹")
                    entries = []
                    if remote:
                        iterator = (
                            (entry.filename, entry) for entry in remote.listdir_iter(target)
                        )
                    else:
                        iterator = ((entry.name, entry.lstat()) for entry in Path(target).iterdir())
                    truncated = False
                    for name, entry in iterator:
                        if len(entries) >= 2000:
                            truncated = True
                            break
                        entries.append(
                            {
                                "name": name,
                                "path": posixpath.join(path, name),
                                "directory": stat.S_ISDIR(entry.st_mode),
                                "symlink": stat.S_ISLNK(entry.st_mode),
                                "size": entry.st_size,
                            }
                        )
                    entries.sort(key=lambda row: (not row["directory"], row["name"].casefold()))
                    return {"root": cwd, "path": path, "entries": entries, "truncated": truncated}
                if not stat.S_ISREG(info.st_mode):
                    raise HTTPException(400, "仅支持预览普通文件")
                if info.st_size > MAX_FILE:
                    raise HTTPException(413, "文件超过 8 MiB，请在终端中查看")
                source = remote.open(target, "rb") if remote else Path(target).open("rb")
                with source:
                    content = source.read(MAX_FILE + 1)
                if len(content) > MAX_FILE:
                    raise HTTPException(413, "文件超过 8 MiB，请在终端中查看")
                mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
                try:
                    text = content.decode("utf-8") if b"\x00" not in content else None
                except UnicodeDecodeError:
                    text = None
                return {
                    "path": path,
                    "name": posixpath.basename(path),
                    "size": len(content),
                    "mime": mime,
                    "text": text,
                    "data_base64": base64.b64encode(content).decode(),
                }
            except HTTPException:
                raise
            except FileNotFoundError as exc:
                raise HTTPException(404, "文件或目录不存在") from exc
            except PermissionError as exc:
                raise HTTPException(403, "服务器账户无权读取此文件") from exc
            except Exception as exc:
                self.close()
                raise HTTPException(502, "读取服务器文件失败：" + str(exc)) from exc


class WorkspaceTools:
    def __init__(self, session):
        self.session = session
        self.files = FileAccess(session)
        self.catalog = HistoryCache(entries=32, max_bytes=8 * 1024 * 1024, ttl=60)
        self.catalog_lock = asyncio.Lock()
        self.terminals = {}
        self.terminal_locks = {}

    async def close(self):
        tasks = []
        for terminal in self.terminals.values():
            if terminal["state"] in ("starting", "running"):
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2):
                        await self.session.rpc(
                            "command/exec/terminate", {"processId": terminal["id"]}
                        )
            if task := terminal.get("task"):
                task.cancel()
                tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.terminals.clear()
        self.catalog.clear()
        await asyncio.to_thread(self.files.close)

    async def thread(self, thread_id):
        return (await self.session.read_thread(thread_id, 1))["thread"]

    async def extensions(self, thread_id, force=False):
        thread = await self.thread(thread_id)
        cwd = thread["cwd"]
        async with self.catalog_lock:
            cached = None if force else self.catalog.get(cwd)
            if cached is not None:
                return cached
            skills_result, plugins_result = await asyncio.gather(
                self.session.rpc("skills/list", {"cwds": [cwd], "forceReload": force}),
                self.session.rpc("plugin/list", {"cwds": [cwd], "forceRefetch": False}),
                return_exceptions=True,
            )
            errors, skills, plugins = [], [], []
            if isinstance(skills_result, Exception):
                errors.append("Skills：" + str(getattr(skills_result, "detail", skills_result)))
            else:
                for entry in skills_result.get("data", []):
                    skills.extend(item for item in entry.get("skills", []) if item.get("enabled"))
                    errors.extend(
                        str(item.get("message", item)) for item in entry.get("errors", [])
                    )
            if isinstance(plugins_result, Exception):
                errors.append("插件：" + str(getattr(plugins_result, "detail", plugins_result)))
            else:
                for market in plugins_result.get("marketplaces", []):
                    plugins.extend(
                        {**item, "marketplace": market.get("name")}
                        for item in market.get("plugins", [])
                        if item.get("installed") and item.get("enabled")
                    )
            result = {
                "skills": list({item["path"]: item for item in skills}.values()),
                "plugins": list({item["id"]: item for item in plugins}.values()),
                "errors": errors,
            }
            if skills or plugins or not errors:
                self.catalog.put(cwd, result)
            return result

    async def inputs(self, thread_id, selections):
        if not selections:
            return []
        catalog = await self.extensions(thread_id)
        skills = {item["path"]: item for item in catalog["skills"]}
        plugins = {item["id"]: item for item in catalog["plugins"]}
        chosen = {}
        for selection in selections:
            if selection.kind == "skill" and selection.id in skills:
                chosen[selection.id] = skills[selection.id]
            elif selection.kind == "plugin" and selection.id in plugins:
                for path, item in skills.items():
                    if item.get("pluginId") == selection.id:
                        chosen[path] = item
            else:
                raise HTTPException(409, "所选插件或 skill 已变化，请刷新列表重新选择")
        if len(chosen) > 32:
            raise HTTPException(422, "请选择具体的 skill，每次最多使用 32 个")
        return [
            {"type": "skill", "name": item["name"], "path": path} for path, item in chosen.items()
        ]

    async def file(self, thread_id, path, directory=False):
        thread = await self.thread(thread_id)
        return await asyncio.to_thread(self.files.read, thread["cwd"], path, directory)

    def terminal_status(self, thread_id):
        terminal = self.terminals.get(thread_id)
        if not terminal:
            return {"state": "closed", "thread_id": thread_id}
        return {key: value for key, value in terminal.items() if key not in ("task", "buffer")} | {
            "output_base64": base64.b64encode(terminal["buffer"]).decode()
        }

    def publish_terminal(self, thread_id):
        terminal = self.terminals[thread_id]
        self.session.publish(
            {
                "method": "deepqueue/terminal",
                "params": {
                    "threadId": thread_id,
                    "id": terminal["id"],
                    "state": terminal["state"],
                    "error": terminal.get("error"),
                    "exit_code": terminal.get("exit_code"),
                },
            }
        )

    def event(self, method, params):
        if method in ("skills/changed", "plugin/changed", "plugins/changed"):
            self.catalog.clear()
        if method != "command/exec/outputDelta":
            return
        for thread_id, terminal in self.terminals.items():
            if terminal["id"] != params.get("processId"):
                continue
            data = base64.b64decode(params["deltaBase64"])
            terminal["offset"] += len(data)
            terminal["buffer"] = (terminal["buffer"] + data)[-128 * 1024 :]
            if terminal["state"] == "starting":
                terminal["state"] = "running"
                self.publish_terminal(thread_id)
            self.session.publish(
                {
                    "method": "deepqueue/terminal/output",
                    "params": {
                        "threadId": thread_id,
                        "id": terminal["id"],
                        "offset": terminal["offset"],
                        "data_base64": params["deltaBase64"],
                    },
                }
            )

    async def start_terminal(self, thread_id, size):
        async with self.terminal_locks.setdefault(thread_id, asyncio.Lock()):
            previous = self.terminals.get(thread_id)
            if previous and previous["state"] in ("starting", "running"):
                return self.terminal_status(thread_id)
            if sum(t["state"] in ("starting", "running") for t in self.terminals.values()) >= 6:
                raise HTTPException(409, "此服务器已打开 6 个终端，请先关闭一个")
            thread = (await self.session.open_thread(thread_id, 1))["thread"]
            for old_id, old in list(self.terminals.items()):
                if len(self.terminals) < 32:
                    break
                if old["state"] not in ("starting", "running"):
                    self.terminals.pop(old_id)
                    self.terminal_locks.pop(old_id, None)
            terminal = {
                "id": "dqterm-" + uuid.uuid4().hex,
                "thread_id": thread_id,
                "cwd": thread["cwd"],
                "state": "starting",
                "buffer": b"",
                "offset": 0,
            }
            self.terminals[thread_id] = terminal
            params = {
                "command": ["bash", "-il"],
                "cwd": thread["cwd"],
                "processId": terminal["id"],
                "tty": True,
                "size": {"cols": size.cols, "rows": size.rows},
                "disableTimeout": True,
                "disableOutputCap": True,
                "env": {"TERM": "xterm-256color"},
            }
            if thread.get("sandbox"):
                params["sandboxPolicy"] = thread["sandbox"]
            client = self.session.require_client()

            async def execute():
                try:
                    result = await client.request("command/exec", params, timeout=24 * 3600)
                    terminal["exit_code"] = result["exitCode"]
                    terminal["state"] = "exited"
                except asyncio.CancelledError:
                    terminal["state"] = "closed"
                    raise
                except Exception as exc:
                    terminal["state"] = "error"
                    terminal["error"] = str(exc) or "终端连接中断"
                finally:
                    self.publish_terminal(thread_id)

            terminal["task"] = asyncio.create_task(execute())
            self.publish_terminal(thread_id)
            return self.terminal_status(thread_id)

    def checked_terminal(self, thread_id, terminal_id):
        self.session.require_client()
        terminal = self.terminals.get(thread_id)
        if (
            not terminal
            or terminal["id"] != terminal_id
            or terminal["state"] not in ("starting", "running")
        ):
            raise HTTPException(409, "终端已结束或连接已变化")
        return terminal

    async def write_terminal(self, thread_id, data):
        self.checked_terminal(thread_id, data.id)
        return await self.session.rpc(
            "command/exec/write",
            {"processId": data.id, "deltaBase64": base64.b64encode(data.data.encode()).decode()},
        )

    async def resize_terminal(self, thread_id, data):
        self.checked_terminal(thread_id, data.id)
        return await self.session.rpc(
            "command/exec/resize",
            {"processId": data.id, "size": {"cols": data.cols, "rows": data.rows}},
        )

    async def stop_terminal(self, thread_id, terminal_id):
        self.checked_terminal(thread_id, terminal_id)
        await self.session.rpc("command/exec/terminate", {"processId": terminal_id})
        return {"ok": True}
