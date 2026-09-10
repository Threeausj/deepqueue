from __future__ import annotations

import getpass
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, urlencode

from .config import private_write
from .models import Server, service_url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def client_path():
    return Path(
        os.environ.get("DEEPQUEUE_CLIENT_CONFIG", "~/.config/deepqueue/client.json")
    ).expanduser()


def client_config(args):
    explicit_url = getattr(args, "url", None)
    if getattr(args, "home", None):
        if explicit_url:
            raise ValueError("Choose --home for a local queue or --url for a remote queue")
        return None
    path = client_path()
    saved = json.loads(path.read_text()) if path.exists() else {}
    url = service_url(explicit_url or os.environ.get("DEEPQUEUE_URL") or saved.get("url"))
    if not url:
        return None
    # Credentials for one queue must never be sent to another URL selected on the command line.
    same_queue = url == saved.get("url")
    token_env = (
        os.environ.get("DEEPQUEUE_TOKEN_ENV")
        or (saved.get("token_env") if same_queue else None)
        or "DEEPQUEUE_TOKEN"
    )
    token = os.environ.get(token_env) or (saved.get("token") if same_queue else None)
    server = (
        getattr(args, "target_server", None)
        or os.environ.get("DEEPQUEUE_SERVER")
        or (saved.get("server") if same_queue else None)
    )
    if server:
        Server(name=server)
    return {"url": url, "server": server, "token": token, "token_env": token_env}


class Client:
    def __init__(self, config):
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, data=None, **query):
        if not self.config.get("token"):
            raise ValueError("Remote queue token is missing; run deepqueue client configure")
        url = self.config["url"] + "/api" + path
        query = {key: value for key, value in query.items() if value is not None}
        if query:
            url += "?" + urlencode(query)
        request = urllib.request.Request(
            url,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + self.config["token"],
                "Content-Type": "application/json",
                "X-DeepQueue": "1",
            },
        )
        try:
            with self.opener.open(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("detail", f"HTTP {exc.code}")
            except (ValueError, AttributeError):
                detail = f"HTTP {exc.code}"
            raise ValueError(f"Remote queue rejected the request: {detail}") from None
        except urllib.error.URLError as exc:
            raise ValueError(f"Remote queue unavailable: {exc.reason}") from None


def configure_client(args):
    if args.action == "show":
        config = client_config(args)
        return {
            "path": str(client_path()),
            **{key: value for key, value in (config or {}).items() if key != "token"},
            "token_configured": bool(config and config.get("token")),
        }
    if args.action == "clear":
        client_path().unlink(missing_ok=True)
        return {"configured": False}
    url = service_url(args.url or os.environ.get("DEEPQUEUE_URL"))
    if not url:
        raise ValueError("Provide --url for the public queue")
    token_env = args.token_env
    if token_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ValueError("Invalid token environment variable")
    token = os.environ.get(token_env) if token_env else getpass.getpass("Queue access token: ")
    config = {"url": url, "token": token, "server": args.target_server}
    identity = Client(config).request("/identity")
    if identity["server"]:
        if args.target_server and args.target_server != identity["server"]:
            raise ValueError("The token belongs to a different execution server")
        config["server"] = identity["server"]
    if not config["server"]:
        raise ValueError("Set --target-server to bind this submission client")
    Server(name=config["server"])
    servers = Client(config).request("/servers")
    if config["server"] not in {row["name"] for row in servers}:
        raise ValueError("Unknown execution server")
    if token_env:
        config.pop("token")
        config["token_env"] = token_env
    private_write(client_path(), json.dumps(config, indent=2))
    return {"configured": True, "url": url, "server": config["server"], "path": str(client_path())}


def bind_server(payload, server, *, batch=False):
    if not isinstance(payload, dict):
        raise ValueError("Submission must be a JSON object")
    payload = deepcopy(payload)
    if not server:
        raise ValueError("Remote submissions require a bound --target-server or client profile")
    if batch:
        defaults = payload.setdefault("defaults", {})
        records = [defaults, *payload.get("experiments", [])]
    else:
        records = [payload]
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Submission records must be objects")
        if record.get("server") not in (None, server):
            raise ValueError(f"Submission targets another server; this client is bound to {server}")
    records[0]["server"] = server
    return payload


def execute_remote(args, config):
    from .cli import bind_origin, job_update_values, job_values, json_file, server_update_values

    client = Client(config)
    command, action = args.command, getattr(args, "action", None)
    if command == "models":
        return client.request("/models", server=config["server"])
    if command == "codex":
        from .gateway import AccessOptions, ForkThread, Message, NewThread, RenameThread, Reply
        from .models import CodexConnection

        if config["server"] not in (None, args.server):
            raise ValueError("The client is bound to a different execution server")
        path = "/servers/" + quote(args.server, safe="") + "/codex"
        if action == "configure":
            data = CodexConnection.model_validate(json_file(args.file))
            return client.request(path + "/settings", data.model_dump())
        if action == "status":
            return client.request(path)
        if action == "connect":
            return client.request(path + "/connect", {"start": args.start})
        if action == "disconnect":
            return client.request(path + "/disconnect", {})
        if action == "models":
            return client.request(path + "/models")
        if action == "permissions":
            return client.request(path + "/permissions", cwd=args.cwd)
        if action == "projects":
            return client.request(path + "/projects", cursor=args.cursor)
        if action == "threads":
            return client.request(path + "/threads", cursor=args.cursor, search=args.search)
        if action == "new":
            return client.request(
                path + "/threads",
                NewThread(
                    cwd=args.cwd,
                    model=args.model,
                    project_id=args.project_id,
                    permissions=args.permissions,
                    approval_policy=args.approval_policy,
                ).model_dump(exclude_none=True),
            )
        if action == "reply":
            return client.request(
                path + "/reply", Reply.model_validate(json_file(args.file)).model_dump()
            )
        thread = path + "/threads/" + quote(args.thread_id, safe="")
        if action == "read":
            return client.request(thread)
        if action == "access":
            data = AccessOptions(permissions=args.permissions, approval_policy=args.approval_policy)
            if not data.access_params():
                raise ValueError("Specify --permissions or --approval-policy")
            return client.request(thread + "/access", data.model_dump(exclude_none=True))
        if action == "fork":
            data = ForkThread(
                last_turn_id=args.last_turn_id,
                model=args.model,
                effort=args.effort,
                request_id=args.request_id or uuid.uuid4().hex,
            )
            return client.request(thread + "/fork", data.model_dump(exclude_none=True))
        if action == "rename":
            return client.request(thread + "/name", RenameThread(name=args.name).model_dump())
        if action == "interrupt":
            return client.request(thread + "/interrupt", {"turn_id": args.turn_id})
        if action == "send":
            data = Message(
                text=args.text,
                model=args.model,
                effort=args.effort,
                request_id=args.request_id or uuid.uuid4().hex,
                permissions=args.permissions,
                approval_policy=args.approval_policy,
            )
            return client.request(thread + "/messages", data.model_dump())
    if command == "server":
        if action == "list":
            rows = client.request("/servers")
            return [row for row in rows if config["server"] in (None, row["name"])]
        if action in ("update", "order") and config["server"] is not None:
            raise ValueError("Server management requires an unscoped administrator client")
        if action == "order":
            return client.request("/servers/order", {"names": args.names})
        if action == "update":
            return client.request(
                "/servers/" + quote(args.name, safe="") + "/settings", server_update_values(args)
            )
    if command == "job":
        if action == "submit":
            values = bind_server(job_values(args, config["server"]), config["server"])
            return client.request("/jobs", bind_origin(values, args))
        if action == "list":
            server = args.server or config["server"]
            if config["server"] and server != config["server"]:
                raise ValueError("The client is bound to a different execution server")
            return client.request("/jobs", server=server, status=args.status, limit=args.limit)
        path = "/jobs/" + quote(args.id, safe="")
        job = client.request(path)
        if config["server"] and job["server"] != config["server"]:
            raise ValueError("The job belongs to a different execution server")
        if action == "show":
            return job
        if action == "update":
            return client.request(path + "/settings", job_update_values(args))
        if action in ("links", "tmux", "logs"):
            endpoint = "terminal" if action == "tmux" else action
            result = client.request(
                path + "/" + endpoint,
                **({"size": max(1, min(args.bytes, 262144))} if action == "logs" else {}),
            )
            if action == "logs" and args.raw:
                print(result["text"], end="")
                return None
            return result
        control = {"action": action}
        if action == "priority":
            control["priority"] = args.value
        elif action == "mode":
            control["completion_mode"] = args.value
            if args.max_improvement_rounds is not None:
                control["max_improvement_rounds"] = args.max_improvement_rounds
        elif action == "retry-agent":
            control["phase"] = args.phase
        elif action == "approve" and args.resources:
            control["resources"] = json_file(args.resources)
        if action not in (
            "pause",
            "resume",
            "cancel",
            "priority",
            "mode",
            "retry-agent",
            "approve",
        ):
            raise ValueError("This operation must be performed by the queue host operator")
        return client.request(path + "/control", control)
    if command == "batch":
        if action in ("preview", "submit"):
            values = bind_server(json_file(args.file), config["server"], batch=True)
            payload = bind_origin(values, args, batch=True)
            return client.request(
                "/batches/preview" if action == "preview" else "/batches", payload
            )
        server = args.server or config["server"]
        if config["server"] and server != config["server"]:
            raise ValueError("The client is bound to a different execution server")
        if action == "list":
            return client.request("/batches", server=server)
        batch = client.request("/batch", name=args.name, server=server)
        if config["server"] and any(
            job["server"] != config["server"] for job in batch["experiments"]
        ):
            raise ValueError("The batch contains another server's experiments")
        if action == "show":
            return batch
        if action == "report":
            return client.request("/batch/report", name=args.name, server=server)
        control = {"action": action}
        if action == "priority":
            control["priority"] = args.value
        client.request("/batch/control", control, name=args.name, server=server)
        return client.request("/batch", name=args.name, server=server)
    raise ValueError(
        "This is a remote submission client; use --home on the queue host to administer it"
    )
