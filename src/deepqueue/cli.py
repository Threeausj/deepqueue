from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import json
import logging
import os
import signal
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from .access import Access
from .agent import AppServer, model_catalog
from .config import Secrets, initialize, load_settings, state_home, update_settings
from .db import SCHEMA_VERSION, Database
from .models import (
    AGENT_PHASES,
    AGENT_SETTING_FIELDS,
    STATUSES,
    CodexConnection,
    JobSpec,
    JobUpdate,
    Resources,
    Server,
)
from .remote import client_config, configure_client, execute_remote
from .scheduler import Scheduler
from .servers import ServerUpdate, update_server
from .skills import install_skill
from .transport import Transport, fingerprint, host_key, trust_host
from .worker import identity


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def json_file(path):
    return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())


def server_update_values(args):
    changes = json_file(args.file) if args.file else {}
    changes.update(
        {
            key: getattr(args, key)
            for key in ("display_name", "host", "port", "username", "max_running", "fingerprint")
            if getattr(args, key) is not None
        }
    )
    return ServerUpdate.model_validate(changes).model_dump(exclude_unset=True)


def origin_options(command):
    command.add_argument(
        "--from-agent", action="store_true", help="Bind results to the current CODEX_THREAD_ID"
    )
    command.add_argument("--source-thread-id", help="Codex conversation receiving the result")
    command.add_argument("--completion-mode", choices=["finish", "improve"])
    command.add_argument("--max-improvement-rounds", type=int)
    command.add_argument("--launch-max-retries", type=int)
    command.add_argument("--baseline-job-id")
    agent_options(command)


def agent_options(command):
    for phase in AGENT_PHASES:
        command.add_argument(
            f"--{phase}-model",
            help="Codex model ID"
            + (", or source-thread for the conversation model" if phase == "archive" else ""),
        )
        command.add_argument(
            f"--{phase}-effort",
            help="Reasoning effort" + (", or source-thread" if phase == "archive" else ""),
        )


def bind_origin(payload, args, *, batch=False):
    payload = deepcopy(payload)
    requested = args.source_thread_id
    # Only the submitting agent opts into environment binding. A web/daemon process may
    # inherit the developer's CODEX_THREAD_ID and must never use it as a submission origin.
    current = os.environ.get("CODEX_THREAD_ID") if args.from_agent else None
    if requested and current and requested != current:
        raise ValueError("--source-thread-id differs from the current CODEX_THREAD_ID")
    requested = requested or current
    if not isinstance(payload, dict):
        raise ValueError("Submission must be a JSON object")
    if batch:
        defaults = payload.setdefault("defaults", {})
        if not isinstance(defaults, dict) or not isinstance(payload.get("experiments"), list):
            raise ValueError("Batch requires object defaults and an experiments array")
        records = [defaults, *payload["experiments"]]
    else:
        records = [payload]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Each experiment must be a JSON object")
    for key in (
        "completion_mode",
        "max_improvement_rounds",
        "parent_job_id",
        "launch_max_retries",
        "baseline_job_id",
    ):
        value = getattr(args, key, None)
        if value is not None:
            for record in records:
                record[key] = value
    for option, field in (("model", "agent_models"), ("effort", "agent_efforts")):
        selected = {
            phase: getattr(args, f"{phase}_{option}", None)
            for phase in AGENT_PHASES
            if getattr(args, f"{phase}_{option}", None) is not None
        }
        if selected:
            for record in records:
                declared_options = record.get(field, {})
                if not isinstance(declared_options, dict):
                    raise ValueError(f"{field} must be an object")
                record[field] = {**declared_options, **selected}
    if any(
        record.get("source_thread_id") is not None
        and not isinstance(record["source_thread_id"], str)
        for record in records
    ):
        raise ValueError("source_thread_id must be a conversation ID string")
    declared = {record.get("source_thread_id") for record in records} - {None}
    if args.from_agent and not requested:
        raise ValueError("CODEX_THREAD_ID is missing; supply the real --source-thread-id")
    if requested:
        if declared - {requested}:
            raise ValueError(
                "Submission source_thread_id conflicts with the submitting conversation"
            )
        records[0]["source_thread_id"] = requested
        if batch:
            for record in records[1:]:
                if "source_thread_id" in record:
                    record["source_thread_id"] = requested
    return payload


def job_update_values(args):
    values = json_file(args.file) if args.file else {}
    if not isinstance(values, dict):
        raise ValueError("Job update must be a JSON object")
    for field in (
        "completion_mode",
        "max_improvement_rounds",
        "launch_max_retries",
        "baseline_job_id",
        "intent",
    ):
        if getattr(args, field) is not None:
            values[field] = getattr(args, field)
    if args.shell_command is not None:
        values["command"] = args.shell_command
    if args.resources:
        values["resources"] = json_file(args.resources)
    for option, field in (("model", "agent_models"), ("effort", "agent_efforts")):
        selected = {
            phase: getattr(args, f"{phase}_{option}")
            for phase in AGENT_PHASES
            if getattr(args, f"{phase}_{option}") is not None
        }
        if selected:
            if not isinstance(values.get(field, {}), dict):
                raise ValueError(f"{field} must be an object")
            values[field] = {**values.get(field, {}), **selected}
    return JobUpdate.model_validate(values).model_dump(exclude_unset=True)


def job_values(args, default_server="local"):
    if args.file:
        return json_file(args.file)
    if not args.shell_command:
        raise ValueError("Provide --file or --command")
    return dict(
        server=args.server or default_server,
        cwd=args.cwd,
        title=args.title,
        command=args.shell_command,
        intent=args.intent,
        priority=args.priority,
        resources=json_file(args.resources) if args.resources else None,
        context_files=args.context,
        artifact_files=args.artifact,
        depends_on=args.depends_on,
        idempotency_key=args.idempotency_key,
        skip_estimate=args.skip_estimate,
        archive=not args.no_archive,
        timeout_seconds=args.timeout,
    )


def daemon_state(home):
    path = home / "scheduler.lock"
    if not path.exists():
        return {"running": False}
    with path.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            record = json.loads(lock.read() or "{}")
            heartbeat = home / "heartbeat.json"
            return {
                "running": True,
                "owner": record,
                "heartbeat": json.loads(heartbeat.read_text()) if heartbeat.exists() else None,
            }
    return {"running": False}


def parser():
    root = argparse.ArgumentParser(
        prog="deepqueue", description="Persistent experiment command queues"
    )
    root.add_argument("--home", help="State directory (or DEEPQUEUE_HOME)")
    root.add_argument("--url", help="Public queue HTTPS URL (or DEEPQUEUE_URL)")
    root.add_argument("--target-server", help="Bind the remote client and skill to this server")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize the queue and register the local server")
    sub.add_parser("doctor", help="Check local app-server, Luna availability, and skill discovery")
    sub.add_parser("models", help="List Codex model choices for each agent stage")
    codex = sub.add_parser("codex", help="Manage a server's web Codex workspace").add_subparsers(
        dest="action", required=True
    )
    for action in (
        "configure",
        "status",
        "connect",
        "disconnect",
        "models",
        "permissions",
        "projects",
        "threads",
        "read",
        "new",
        "send",
        "access",
        "fork",
        "rename",
        "interrupt",
        "reply",
    ):
        command = codex.add_parser(action)
        command.add_argument("server")
        if action in ("configure", "reply"):
            command.add_argument("--file", required=True, help="JSON file, or - for stdin")
        if action == "connect":
            command.add_argument(
                "--start", action="store_true", help="Start the shared Codex daemon"
            )
        if action in ("threads", "projects"):
            command.add_argument("--cursor")
        if action == "threads":
            command.add_argument("--search", default="")
        if action in ("read", "send", "interrupt", "access", "fork", "rename"):
            command.add_argument("thread_id")
        if action == "permissions":
            command.add_argument("--cwd")
        if action in ("new", "send", "access"):
            command.add_argument("--permissions", help="Server permission profile ID")
            command.add_argument("--approval-policy", choices=["untrusted", "on-request", "never"])
        if action == "rename":
            command.add_argument("--name", required=True)
        if action == "fork":
            command.add_argument(
                "--last-turn-id", help="Fork through this completed turn, inclusive"
            )
            command.add_argument("--model")
            command.add_argument("--effort")
            command.add_argument("--request-id", help="Stable ID to deduplicate a repeated fork")
        if action == "new":
            command.add_argument("--cwd", required=True)
            command.add_argument("--model")
            command.add_argument("--project-id")
        if action == "send":
            command.add_argument("--text", required=True)
            command.add_argument("--model")
            command.add_argument("--effort")
            command.add_argument("--request-id", help="Stable ID to deduplicate a repeated send")
        if action == "interrupt":
            command.add_argument("--turn-id", required=True)
    web = sub.add_parser("web", help="Serve the queue dashboard")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    config = sub.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("show")
    setting = config.add_parser("set")
    setting.add_argument("key")
    setting.add_argument("value", help="JSON value, or an unquoted string")

    client = sub.add_parser("client").add_subparsers(dest="action", required=True)
    connect = client.add_parser("configure")
    connect.add_argument("--token-env", help="Read credentials from this environment variable")
    client.add_parser("show")
    client.add_parser("clear")
    access = sub.add_parser("access").add_subparsers(dest="action", required=True)
    create = access.add_parser("create")
    scope = create.add_mutually_exclusive_group(required=True)
    scope.add_argument("--admin", action="store_true")
    scope.add_argument("--server")
    create.add_argument("--label", default="CLI credential")
    access.add_parser("list")
    revoke = access.add_parser("revoke")
    revoke.add_argument("id")

    server = sub.add_parser("server").add_subparsers(dest="action", required=True)
    server.add_parser("list")
    order = server.add_parser("order", help="Set the shared server display order")
    order.add_argument("names", nargs="+")
    update = server.add_parser("update", help="Edit server metadata or SSH settings")
    update.add_argument("name")
    update.add_argument("--file", help="ServerUpdate JSON, or - for stdin")
    update.add_argument("--display-name")
    update.add_argument("--host")
    update.add_argument("--port", type=int)
    update.add_argument("--user", dest="username")
    update.add_argument("--max-running", type=int)
    update.add_argument("--fingerprint")
    add = server.add_parser("add")
    add.add_argument("name")
    add.add_argument("--host", required=True)
    add.add_argument("--user", required=True)
    add.add_argument("--port", type=int, default=22)
    auth = add.add_mutually_exclusive_group()
    auth.add_argument("--key")
    auth.add_argument("--password", action="store_true", help="Prompt for an encrypted password")
    auth.add_argument("--password-env", help="Read this environment variable in the scheduler")
    add.add_argument(
        "--passphrase", action="store_true", help="Prompt for an encrypted key passphrase"
    )
    add.add_argument("--worker-root", default=".local/share/deepqueue-worker")
    add.add_argument("--max-running", type=int, default=8)
    add.add_argument(
        "--return-agent-socket", help="Forwarded source app-server socket on queue host"
    )
    add.add_argument(
        "--gpu", action="append", default=[], help="Allowed physical GPU index or UUID"
    )
    for action in ("probe", "pause", "resume"):
        item = server.add_parser(action)
        item.add_argument("name")
    callback = server.add_parser("callback")
    callback.add_argument("name")
    callback.add_argument(
        "--socket", help="Forwarded Unix socket; omit to use server Codex or the global default"
    )
    for action in ("fingerprint", "trust"):
        item = server.add_parser(action)
        item.add_argument("host")
        item.add_argument("--port", type=int, default=22)
        if action == "trust":
            item.add_argument("--fingerprint", required=True)

    job = sub.add_parser("job").add_subparsers(dest="action", required=True)
    submit = job.add_parser("submit")
    origin_options(submit)
    submit.add_argument("--file", help="Job JSON, or - for stdin")
    submit.add_argument("--server")
    submit.add_argument("--cwd", default=str(Path.cwd()))
    submit.add_argument("--title", default="Command")
    submit.add_argument("--command", dest="shell_command")
    submit.add_argument("--intent", default="")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--resources", help="Resources JSON file")
    submit.add_argument("--context", action="append", default=[])
    submit.add_argument("--artifact", action="append", default=[])
    submit.add_argument("--depends-on", action="append", default=[])
    submit.add_argument("--idempotency-key")
    submit.add_argument("--skip-estimate", action="store_true")
    submit.add_argument("--no-archive", action="store_true")
    submit.add_argument("--timeout", type=int, default=0)
    submit.add_argument("--parent-job-id", help="Completed experiment being improved")
    listing = job.add_parser("list")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--server")
    listing.add_argument("--limit", type=int, default=100)
    update = job.add_parser("update", help="Adjust a job's future stages and completion policy")
    update.add_argument("id")
    update.add_argument("--file", help="Partial job settings JSON, or - for stdin")
    update.add_argument("--completion-mode", choices=["finish", "improve"])
    update.add_argument("--max-improvement-rounds", type=int)
    update.add_argument("--launch-max-retries", type=int)
    update.add_argument("--baseline-job-id")
    update.add_argument("--intent")
    update.add_argument("--command", dest="shell_command")
    update.add_argument("--resources", help="Replacement resource request JSON file")
    agent_options(update)
    for action in (
        "show",
        "cancel",
        "pause",
        "resume",
        "priority",
        "links",
        "logs",
        "approve",
        "retry-agent",
        "resolve",
        "mode",
        "tmux",
    ):
        item = job.add_parser(action)
        item.add_argument("id")
        if action == "priority":
            item.add_argument("value", type=int, help="Scheduling priority, from -100 to 100")
        if action == "logs":
            item.add_argument("--bytes", type=int, default=65536)
            item.add_argument("--raw", action="store_true")
        if action == "approve":
            item.add_argument("--resources")
        if action == "retry-agent":
            item.add_argument("--phase", choices=["estimate", "launch", "archive"], required=True)
        if action == "mode":
            item.add_argument("value", choices=["finish", "improve"])
            item.add_argument("--max-improvement-rounds", type=int)
        if action == "resolve":
            item.add_argument(
                "--outcome", choices=["succeeded", "failed", "cancelled"], required=True
            )
            item.add_argument(
                "--note", required=True, help="Evidence that the remote process has stopped"
            )

    batch = sub.add_parser("batch").add_subparsers(dest="action", required=True)
    batch.add_parser("list").add_argument("--server")
    for action in ("preview", "submit"):
        batch_input = batch.add_parser(action)
        origin_options(batch_input)
        batch_input.add_argument("file", help="Batch JSON file, or - for stdin")
    for action in ("show", "cancel", "pause", "resume", "priority", "report"):
        item = batch.add_parser(action)
        item.add_argument("name")
        item.add_argument("--server", help="Only this server's experiments in the batch")
        if action == "priority":
            item.add_argument("value", type=int, help="Priority for jobs that have not started")

    daemon = sub.add_parser("daemon").add_subparsers(dest="action", required=True)
    run = daemon.add_parser("run")
    run.add_argument("--once", action="store_true")
    for action in ("start", "stop", "status"):
        daemon.add_parser(action)
    sub.add_parser("schema", help="Print the validated job input JSON Schema")
    skill = sub.add_parser("skill").add_subparsers(dest="action", required=True)
    install = skill.add_parser("install")
    install.add_argument("--path", default=str(Path.home() / ".agents" / "skills" / "deepqueue"))
    return root


async def doctor(home, settings):
    async with AppServer(settings, home / "diagnostics") as agent:
        models = await agent.models()
        skills = await agent.request(
            "skills/list", {"cwds": [str(Path.cwd())], "forceReload": True}
        )
    available = any(
        item.get("model") == settings.model or item.get("id") == settings.model for item in models
    )
    discovered = []
    for item in skills.get("data", []):
        for skill in item.get("skills", []):
            if skill.get("name") == "deepqueue":
                discovered.append(
                    {
                        "name": skill["name"],
                        "path": skill.get("path"),
                        "enabled": skill.get("enabled"),
                    }
                )
    return {
        "app_server": "connected",
        "inference_verified": False,
        "configured_model": settings.model,
        "model_available": available,
        "luna_models": [
            {"id": item.get("id"), "model": item.get("model"), "name": item.get("displayName")}
            for item in models
            if "luna" in (item.get("id", "") + item.get("model", "")).lower()
        ],
        "skills": discovered,
        "daemon": daemon_state(home),
    }


def execute(args):
    if args.command == "client":
        return configure_client(args)
    if args.command == "schema":
        return JobSpec.model_json_schema()
    if args.command == "skill":
        destination = Path(args.path).expanduser().resolve()
        remote = client_config(args)
        return install_skill(
            destination, remote["url"] if remote else None, remote["server"] if remote else None
        )
    remote = client_config(args)
    if remote:
        return execute_remote(args, remote)
    home = state_home(args.home)
    if args.command == "init":
        initialize(home)
        db = Database(home)
        if db.schema_version() != SCHEMA_VERSION and daemon_state(home)["running"]:
            raise ValueError("Stop the daemon before upgrading the database, then restart it")
        db.initialize()
        if not any(row["name"] == "local" for row in db.servers()):
            db.add_server(Server(name="local", worker_root=str(home / "workers" / "local")))
        return {"home": str(home), "database": str(db.path), "model": load_settings(home).model}
    settings = load_settings(home)
    db = Database(home)
    if not (args.command == "daemon" and args.action in ("stop", "status")):
        if db.schema_version() != SCHEMA_VERSION and daemon_state(home)["running"]:
            raise ValueError("Stop the daemon before upgrading the database, then restart it")
        db.ensure_schema()
    if args.command == "doctor":
        return asyncio.run(doctor(home, settings))
    if args.command == "models":
        return asyncio.run(model_catalog(settings))
    if args.command == "codex":
        if args.action == "configure":
            db.set_server_codex(args.server, CodexConnection.model_validate(json_file(args.file)))
            return db.server(args.server)["config"]["codex"]
        raise ValueError("Use --url and an administrator token to control the web Codex workspace")
    if args.command == "web":
        import uvicorn

        from .web import create_app

        if args.host not in ("127.0.0.1", "localhost", "::1") and (
            not settings.public_url or not Access(home).has_admin()
        ):
            raise ValueError("Non-loopback binding requires a public URL and administrator token")
        uvicorn.run(create_app(home), host=args.host, port=args.port, timeout_graceful_shutdown=5)
        return None
    if args.command == "access":
        access = Access(home)
        if args.action == "list":
            return access.list()
        if args.action == "revoke":
            access.revoke(args.id)
            return {"revoked": args.id}
        if args.server:
            db.server(args.server)
            if not access.has_admin():
                raise ValueError("Create an administrator token first")
        return access.create(args.label, args.server)
    if args.command == "config":
        if args.action == "show":
            return {"home": str(home), **settings.model_dump()}
        try:
            value = json.loads(args.value)
        except ValueError:
            value = args.value
        updated = update_settings(home, {args.key: value})
        return {
            "config": updated.model_dump(),
            "restart_daemon_to_apply": args.key not in AGENT_SETTING_FIELDS,
        }
    if args.command == "server":
        if args.action == "list":
            return db.servers()
        if args.action == "order":
            db.reorder_servers(args.names)
            return db.servers()
        if args.action == "update":
            return update_server(
                home, args.name, ServerUpdate.model_validate(server_update_values(args))
            )
        if args.action == "fingerprint":
            key = host_key(args.host, args.port)
            return {
                "host": args.host,
                "port": args.port,
                "fingerprint": fingerprint(key),
                "key_type": key.get_name(),
                "trusted": False,
            }
        if args.action == "trust":
            return trust_host(home, args.host, args.port, args.fingerprint)
        if args.action == "add":
            vault = Secrets(home)
            password_ref = "env:" + args.password_env if args.password_env else None
            if args.password:
                password_ref = vault.put(getpass.getpass("SSH password: "))
            passphrase_ref = (
                vault.put(getpass.getpass("SSH key passphrase: ")) if args.passphrase else None
            )
            server = Server(
                name=args.name,
                kind="ssh",
                host=args.host,
                username=args.user,
                port=args.port,
                key_file=str(Path(args.key).expanduser().resolve()) if args.key else None,
                password_ref=password_ref,
                passphrase_ref=passphrase_ref,
                worker_root=args.worker_root,
                max_running=args.max_running,
                gpu_allowlist=args.gpu,
                return_agent_socket=args.return_agent_socket,
            )
            db.add_server(server)
            return db.server(args.name)
        if args.action in ("pause", "resume"):
            db.enable_server(args.name, args.action == "resume")
            return db.server(args.name)
        if args.action == "callback":
            db.set_server_callback(args.name, args.socket)
            return db.server(args.name)
        server = Server.model_validate(db.server(args.name)["config"])
        result = Transport(home, server).call("probe")
        result["received_at"] = time.time()
        if not db.snapshot(server.name, result, expected_server=server):
            raise ValueError("服务器设置已变化，请重新探测")
        return result
    if args.command == "job":
        if args.action == "submit":
            return db.submit(JobSpec.model_validate(bind_origin(job_values(args), args)))
        if args.action == "list":
            return db.jobs(
                [args.status] if args.status else None, args.server, max(1, min(args.limit, 10000))
            )
        if args.action == "show":
            return db.detail(args.id)
        if args.action == "cancel":
            db.cancel(args.id)
        elif args.action in ("pause", "resume"):
            db.set_paused(args.id, args.action == "pause")
        elif args.action == "priority":
            db.set_priority(args.id, args.value)
        elif args.action == "mode":
            db.set_completion_mode(args.id, args.value, args.max_improvement_rounds)
        elif args.action == "update":
            db.update_job(args.id, job_update_values(args))
        elif args.action == "tmux":
            job = db.job(args.id)
            if not job["run_id"]:
                return {"available": False}
            server = Server.model_validate(db.server(job["server"])["config"])
            return Transport(home, server).terminal(job["run_id"])
        elif args.action == "approve":
            db.approve(
                args.id,
                Resources.model_validate(json_file(args.resources)) if args.resources else None,
            )
        elif args.action == "retry-agent":
            db.retry_agent(args.id, args.phase)
        elif args.action == "resolve":
            if db.job(args.id)["status"] != "lost":
                raise ValueError("Only lost jobs can be resolved")
            db.finish(
                args.id,
                {"status": args.outcome, "resolved_by": "operator", "note": args.note},
                resolve=True,
            )
        elif args.action == "links":
            detail = db.detail(args.id)
            source = detail["spec"].get("source_thread_id")
            origin = (
                [
                    {
                        "phase": "source",
                        "status": detail["archive_status"],
                        "url": settings.link_template.format(thread_id=quote(source, safe="")),
                        "thread_id": source,
                        "resume": f"codex resume {source}",
                    }
                ]
                if source
                else []
            )
            return origin + [
                {
                    "phase": item["phase"],
                    "status": item["status"],
                    "url": item["deep_link"],
                    "thread_id": item["thread_id"],
                    "resume": f"codex resume {item['thread_id']}" if item["thread_id"] else None,
                }
                for item in detail["agents"]
            ]
        elif args.action == "logs":
            job = db.job(args.id)
            if not job["run_id"]:
                return {"text": "", "bytes": 0, "truncated": False}
            server = Server.model_validate(db.server(job["server"])["config"])
            result = Transport(home, server).call("logs", run_id=job["run_id"], limit=args.bytes)
            if args.raw:
                print(result["text"], end="")
                return None
            return result
        return db.job(args.id)
    if args.command == "batch":
        if args.action == "preview":
            return db.preview_batch(bind_origin(json_file(args.file), args, batch=True))
        if args.action == "submit":
            return db.submit_batch(bind_origin(json_file(args.file), args, batch=True))
        if args.action == "list":
            return db.batches(args.server)
        if args.action == "cancel":
            db.cancel_batch(args.name, args.server)
        elif args.action in ("pause", "resume"):
            db.set_batch_paused(args.name, args.action == "pause", args.server)
        elif args.action == "priority":
            control = db.set_batch_priority(args.name, args.value, args.server)
        batch = db.batch(args.name, args.server)
        if args.action == "priority":
            batch["control"] = control
        if args.action == "report":
            batch["experiments"] = [
                {
                    "key": job["key"],
                    "parameters": job["spec"].get("parameters", {}),
                    "id": job["id"],
                    "status": job["status"],
                    "priority": job["priority"],
                    "paused": job["paused"],
                    "pause_sources": job["pause_sources"],
                    "archive_status": job["archive_status"],
                    "reason": job["reason"],
                    "resources": job["resources"],
                    "analysis": job["analysis"],
                    "execution": db.run(job["run_id"])["result"] if job["run_id"] else None,
                    "artifact_files": job["spec"]["artifact_files"],
                    "agents": [
                        {"phase": a["phase"], "url": a["deep_link"]}
                        for a in db.detail(job["id"])["agents"]
                    ],
                }
                for job in batch["experiments"]
            ]
        counts = {}
        for job in batch["experiments"]:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        batch["counts"] = counts
        return batch
    if args.command == "daemon":
        if args.action == "status":
            return daemon_state(home)
        if args.action == "run":
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            asyncio.run(Scheduler(home, settings).run(once=args.once))
            return {"stopped": True}
        if args.action == "start":
            status = daemon_state(home)
            if status["running"]:
                return status
            with (home / "scheduler.log").open("ab") as output:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "deepqueue", "--home", str(home), "daemon", "run"],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                )
            for _ in range(50):
                status = daemon_state(home)
                if status["running"]:
                    return status
                if proc.poll() is not None:
                    raise RuntimeError(f"Scheduler failed to start; see {home / 'scheduler.log'}")
                time.sleep(0.1)
            raise RuntimeError(
                f"Scheduler did not acquire its lock; inspect {home / 'scheduler.log'}"
            )
        status = daemon_state(home)
        if status["running"]:
            owner = status["owner"]
            if identity(owner["pid"]) != owner:
                raise ValueError("Scheduler identity mismatch; refusing to signal a reused PID")
            os.kill(owner["pid"], signal.SIGTERM)
            for _ in range(100):
                if not daemon_state(home)["running"]:
                    return {"running": False, "experiments_continue": True}
                time.sleep(0.1)
            return {"stopping": True, "experiments_continue": True}
        return status
    raise ValueError("Unknown command")


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        result = execute(args)
        if result is not None:
            emit(result)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(1)
