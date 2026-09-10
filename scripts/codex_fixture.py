"""Deterministic app-server protocol fixture. No model calls or user data."""

import asyncio
import base64
import copy
import json
import time


class CodexFixture:
    def __init__(self, name="fixture"):
        self.name = name
        self.threads = {}
        self.projects = {}
        self.projects_supported = True
        self.clients = set()
        self.calls = []
        self.replies = {}
        self.tasks = set()
        self.terminals = {}
        self.namespace_failure = False
        self.skills = []
        self.plugins = []
        self.profiles = [
            {"id": ":read-only", "allowed": True},
            {"id": ":workspace", "allowed": True},
            {"id": ":danger-full-access", "allowed": True},
            {"id": "restricted-by-admin", "allowed": False},
        ]
        self.requirements = None
        self.add_thread(f"{name}-task", "Codex 协作示例（协议测试）")

    def add_thread(self, thread_id, name=None, cwd="/tmp/codex-fixture"):
        self.threads[thread_id] = {
            "id": thread_id,
            "name": name,
            "preview": "协议测试任务",
            "cwd": cwd,
            "projectId": None,
            "source": "appServer",
            "model": "gpt-5.6-luna",
            "reasoningEffort": "high",
            "activePermissionProfile": {"id": ":workspace"},
            "sandbox": {"type": "workspaceWrite", "networkAccess": False, "writableRoots": [cwd]},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "updatedAt": int(time.time()),
            "status": {"type": "idle"},
            "turns": [],
        }
        return self.threads[thread_id]

    def add_project(self, project_id, name, roots):
        self.projects[project_id] = {
            "id": project_id,
            "name": name,
            "roots": [{"path": root} for root in roots],
            "position": len(self.projects),
        }
        return self.projects[project_id]

    def configure(self, thread, params):
        if params.get("permissions"):
            profile = next((v for v in self.profiles if v["id"] == params["permissions"]), None)
            if not profile or not profile["allowed"]:
                raise ValueError("Permission profile is not allowed by this server")
        policy = params.get("approvalPolicy")
        if (
            policy
            and self.requirements
            and policy not in self.requirements["allowedApprovalPolicies"]
        ):
            raise ValueError("Approval policy is not allowed by this server")
        if profile_id := params.get("permissions"):
            thread["activePermissionProfile"] = {"id": profile_id}
            thread["sandbox"] = {
                "type": {
                    ":read-only": "readOnly",
                    ":workspace": "workspaceWrite",
                    ":danger-full-access": "dangerFullAccess",
                }[profile_id],
            }
        if sandbox := params.get("sandboxPolicy"):
            thread["sandbox"] = sandbox
            thread["activePermissionProfile"] = None
        if params.get("sandbox") == "read-only":
            thread["sandbox"] = {"type": "readOnly"}
            thread["activePermissionProfile"] = None
        for field in ("model", "approvalPolicy", "approvalsReviewer", "cwd"):
            if params.get(field) is not None:
                thread[field] = params[field]
        if effort := params.get("effort") or params.get("config", {}).get("model_reasoning_effort"):
            thread["reasoningEffort"] = effort

    def settings(self, thread):
        return {
            k: thread[k]
            for k in (
                "model",
                "reasoningEffort",
                "activePermissionProfile",
                "sandbox",
                "approvalPolicy",
                "approvalsReviewer",
                "cwd",
            )
        }

    async def emit(self, method, params, **extra):
        message = json.dumps({"method": method, "params": params, **extra})
        await asyncio.gather(*(s.send(message) for s in self.clients), return_exceptions=True)

    async def handle(self, socket):
        self.clients.add(socket)
        try:
            async for raw in socket:
                message = json.loads(raw)
                self.calls.append(message)
                if "method" not in message:
                    future = self.replies.get(message["id"])
                    if future and not future.done():
                        future.set_result(message)
                    continue
                method, p = message["method"], message.get("params", {})
                if method == "initialized":
                    continue
                thread = self.threads.get(p.get("threadId"))
                if "threadId" in p and thread is None:
                    await socket.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "error": {
                                    "code": -32000,
                                    "message": "Unknown task on this server",
                                },
                            }
                        )
                    )
                    continue
                if method in (
                    "thread/start",
                    "thread/settings/update",
                    "turn/start",
                    "thread/fork",
                ):
                    try:
                        self.configure({}, p)  # Validate before mutating any task.
                    except ValueError as exc:
                        await socket.send(
                            json.dumps(
                                {
                                    "id": message["id"],
                                    "error": {
                                        "code": -32602,
                                        "message": str(exc),
                                    },
                                }
                            )
                        )
                        continue
                if method == "initialize":
                    result = {
                        "userAgent": "DeepQueue protocol fixture (no inference)",
                        "codexHome": "/tmp/codex-fixture-home/.codex",
                    }
                elif method == "skills/list":
                    result = {"data": [{"cwd": p["cwds"][0], "skills": self.skills, "errors": []}]}
                elif method == "plugin/list":
                    result = {"marketplaces": [{"name": "fixture", "plugins": self.plugins}]}
                elif method == "command/exec":
                    if (
                        self.namespace_failure
                        and p.get("sandboxPolicy", {}).get("type") != "dangerFullAccess"
                    ):
                        failure = "bwrap: No permissions to create new namespace\r\n"
                        await self.emit(
                            "command/exec/outputDelta",
                            {
                                "processId": p["processId"],
                                "stream": "stderr",
                                "deltaBase64": base64.b64encode(failure.encode()).decode(),
                                "capReached": False,
                            },
                        )
                        await socket.send(
                            json.dumps(
                                {
                                    "id": message["id"],
                                    "result": {
                                        "exitCode": 1,
                                        "stdout": "",
                                        "stderr": failure,
                                    },
                                }
                            )
                        )
                        continue
                    self.terminals[p["processId"]] = (socket, message["id"])
                    await self.emit(
                        "command/exec/outputDelta",
                        {
                            "processId": p["processId"],
                            "stream": "stdout",
                            "deltaBase64": base64.b64encode(
                                b"Fixture terminal (no execution)\r\n$ "
                            ).decode(),
                            "capReached": False,
                        },
                    )
                    continue
                elif method == "command/exec/write":
                    await self.emit(
                        "command/exec/outputDelta",
                        {
                            "processId": p["processId"],
                            "stream": "stdout",
                            "deltaBase64": p["deltaBase64"],
                            "capReached": False,
                        },
                    )
                    result = {}
                elif method == "command/exec/resize":
                    result = {}
                elif method == "command/exec/terminate":
                    source, request_id = self.terminals.pop(p["processId"])
                    await source.send(
                        json.dumps(
                            {
                                "id": request_id,
                                "result": {
                                    "exitCode": 0,
                                    "stdout": "",
                                    "stderr": "",
                                },
                            }
                        )
                    )
                    result = {}
                elif method == "permissionProfile/list":
                    result = {"data": self.profiles, "nextCursor": None}
                elif method == "configRequirements/read":
                    result = {"requirements": self.requirements}
                elif method == "model/list":
                    result = {
                        "data": [
                            {
                                "id": "gpt-5.6-luna",
                                "model": "gpt-5.6-luna",
                                "displayName": "Luna",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low"},
                                    {"reasoningEffort": "high"},
                                ],
                            }
                        ],
                        "nextCursor": None,
                    }
                elif method == "project/list":
                    if not self.projects_supported:
                        await socket.send(
                            json.dumps(
                                {
                                    "id": message["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Method not found: project/list",
                                    },
                                }
                            )
                        )
                        continue
                    rows = list(self.projects.values())
                    offset, limit = int(p.get("cursor") or 0), p.get("limit", 100)
                    result = {
                        "data": rows[offset : offset + limit],
                        "nextCursor": str(offset + limit) if len(rows) > offset + limit else None,
                    }
                elif method == "thread/list":
                    rows = [
                        {**t, "turns": []}
                        for t in self.threads.values()
                        if (not p.get("searchTerm") or p["searchTerm"] in (t["name"] or ""))
                        and (not p.get("sourceKinds") or t["source"] in p["sourceKinds"])
                    ]
                    offset = int(p.get("cursor") or 0)
                    limit = p.get("limit", 30)
                    result = {
                        "data": rows[offset : offset + limit],
                        "nextCursor": str(offset + limit) if len(rows) > offset + limit else None,
                    }
                elif method == "thread/start":
                    thread = self.add_thread(f"{self.name}-{len(self.threads) + 1}", cwd=p["cwd"])
                    thread["model"] = p.get("model", "gpt-5.6-luna")
                    thread["projectId"] = p.get("projectId")
                    self.configure(thread, p)
                    result = {"thread": thread, **self.settings(thread)}
                elif method in ("thread/read", "thread/resume"):
                    result = {"thread": thread, **self.settings(thread)}
                elif method == "thread/settings/update":
                    before = copy.deepcopy(self.settings(thread))
                    self.configure(thread, p)
                    if before == self.settings(thread):
                        await socket.send(json.dumps({"id": message["id"], "result": {}}))
                        continue
                    settings = self.settings(thread)
                    settings["sandboxPolicy"] = settings.pop("sandbox")
                    settings["effort"] = settings.pop("reasoningEffort")

                    # The real runtime can acknowledge first and publish resolved settings later.
                    async def publish_settings(tid=thread["id"], values=settings):
                        await asyncio.sleep(0.02)
                        await self.emit(
                            "thread/settings/updated",
                            {
                                "threadId": tid,
                                "threadSettings": values,
                            },
                        )

                    task = asyncio.create_task(publish_settings())
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    result = {}
                elif method == "thread/name/set":
                    thread["name"] = p["name"]
                    await self.emit(
                        "thread/name/updated",
                        {"threadId": thread["id"], "threadName": thread["name"]},
                    )
                    result = {}
                elif method == "thread/metadata/update":
                    thread["projectId"] = p["projectId"] or None
                    result = {"thread": thread}
                elif method == "thread/fork":
                    index = len(thread["turns"]) - 1
                    if p.get("lastTurnId"):
                        index = next(
                            (
                                i
                                for i, t in enumerate(thread["turns"])
                                if t["id"] == p["lastTurnId"]
                            ),
                            -1,
                        )
                    if index < 0 or thread["turns"][index]["status"] == "inProgress":
                        await socket.send(
                            json.dumps(
                                {
                                    "id": message["id"],
                                    "error": {
                                        "code": -32602,
                                        "message": "Fork requires a completed turn in this task",
                                    },
                                }
                            )
                        )
                        continue
                    fork = copy.deepcopy(thread)
                    fork.update(
                        id=f"{self.name}-fork-{len(self.threads)}",
                        forkedFromId=thread["id"],
                        status={"type": "idle"},
                    )
                    fork["turns"] = fork["turns"][: index + 1]
                    self.configure(fork, p)
                    self.threads[fork["id"]] = fork
                    result = {"thread": fork, **self.settings(fork)}
                    await self.emit("thread/started", {"thread": fork})
                elif method == "turn/start":
                    self.configure(thread, p)
                    turn = {
                        "id": f"turn-{len(thread['turns']) + 1}",
                        "status": "inProgress",
                        "items": [
                            {
                                "id": f"user-{len(thread['turns']) + 1}",
                                "type": "userMessage",
                                "content": p["input"],
                            }
                        ],
                    }
                    thread["turns"].append(turn)
                    thread["status"] = {"type": "active"}
                    thread["model"] = p.get("model") or thread["model"]
                    thread["name"] = p["input"][0]["text"][:45]
                    await socket.send(json.dumps({"id": message["id"], "result": {"turn": turn}}))
                    task = asyncio.create_task(self.finish(thread, turn))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    continue
                elif method == "turn/interrupt":
                    turn = next(t for t in thread["turns"] if t["id"] == p["turnId"])
                    turn["status"] = "interrupted"
                    thread["status"] = {"type": "idle"}
                    await self.emit("turn/completed", {"threadId": thread["id"], "turn": turn})
                    result = {}
                else:
                    raise AssertionError(method)
                await socket.send(json.dumps({"id": message["id"], "result": result}))
        finally:
            self.clients.discard(socket)

    async def finish(self, thread, turn):
        tid = thread["id"]
        text = turn["items"][0]["content"][0]["text"]
        await self.emit("turn/started", {"threadId": tid, "turn": copy.deepcopy(turn)})
        await self.emit(
            "item/completed", {"threadId": tid, "turnId": turn["id"], "item": turn["items"][0]}
        )
        await asyncio.sleep(0.1)
        if "审批" in text or "提问" in text:
            request_id = f"request-{tid}-{turn['id']}"
            self.replies[request_id] = asyncio.get_running_loop().create_future()
            method = (
                "item/tool/requestUserInput"
                if "提问" in text
                else "item/commandExecution/requestApproval"
            )
            params = {
                "threadId": tid,
                "turnId": turn["id"],
                "itemId": "command",
                "command": "printf fixture",
                "cwd": thread["cwd"],
                "reason": "协议测试确认，不会执行命令",
            }
            if "提问" in text:
                params["questions"] = [
                    {
                        "id": "direction",
                        "question": "下一步优化什么？",
                        "options": [{"label": "验证精度", "description": "示例"}],
                    }
                ]
            await self.emit(method, params, id=request_id)
            response = await self.replies[request_id]
            await self.emit("serverRequest/resolved", {"threadId": tid, "requestId": request_id})
            text += json.dumps(response.get("result"), ensure_ascii=False)
        if "等待" in text:
            return
        item = {"id": f"answer-{turn['id']}", "type": "agentMessage", "text": ""}
        turn["items"].append(item)
        await self.emit("item/started", {"threadId": tid, "turnId": turn["id"], "item": dict(item)})
        for delta in ["这是 **协议测试** 的实时回复。\n\n", "已收到：" + text]:
            if turn["status"] == "interrupted":
                return
            item["text"] += delta
            await self.emit(
                "item/agentMessage/delta",
                {"threadId": tid, "turnId": turn["id"], "itemId": item["id"], "delta": delta},
            )
            await asyncio.sleep(0.15)
        turn["status"] = "completed"
        thread["status"] = {"type": "idle"}
        await self.emit("item/completed", {"threadId": tid, "turnId": turn["id"], "item": item})
        await self.emit("turn/completed", {"threadId": tid, "turn": turn})

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
