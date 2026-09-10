from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, SecretStr, ValidationError, model_validator

from .access import COOKIE, REMEMBER_SECONDS, Access, LoginThrottled, PasswordThrottle
from .agent import model_catalog
from .config import Secrets, load_settings, update_settings
from .db import SCHEMA_VERSION, Database
from .gateway import CodexGateway
from .gateway import router as codex_router
from .models import (
    AGENT_PHASES,
    AGENT_SETTING_FIELDS,
    PRESTART,
    AgentEfforts,
    AgentModels,
    JobSpec,
    JobUpdate,
    Model,
    Resources,
    Server,
    Settings,
    service_url,
)
from .scheduler import Scheduler
from .servers import ServerUpdate, update_server
from .transport import Transport, fingerprint, host_key, trust_host


class Control(Model):
    action: Literal["pause", "resume", "cancel", "priority", "approve", "retry-agent", "mode"]
    priority: int | None = Field(default=None, ge=-100, le=100)
    resources: Resources | None = None
    phase: Literal["estimate", "launch", "archive"] | None = None
    completion_mode: Literal["finish", "improve"] | None = None
    max_improvement_rounds: int | None = Field(default=None, ge=1, le=100)


class DaemonControl(Model):
    action: Literal["start", "stop"]


class GeneralSettings(Model):
    agent_models: AgentModels
    agent_efforts: AgentEfforts
    launch_max_retries: int = Field(default=3, ge=0, le=10)


class DeploymentSettings(Model):
    public_url: str | None = None


class Login(Model):
    token: SecretStr | None = Field(default=None, min_length=1, max_length=1024)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=128)
    remember: bool = True

    @model_validator(mode="after")
    def one_credential(self):
        if (self.token is None) == (self.password is None):
            raise ValueError("请选择密码或管理员令牌中的一种方式登录")
        return self


class PasswordSettings(Model):
    enabled: bool
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def required_password(self):
        if self.enabled != (self.password is not None):
            raise ValueError("启用密码登录时请填写新密码；关闭时无需新密码")
        return self


class AccessRequest(Model):
    label: str = Field(default="Server skill", min_length=1, max_length=100)


class CallbackSettings(Model):
    return_agent_socket: str | None = None


class RemoteServer(Model):
    name: str
    display_name: str | None = None
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    key_file: str | None = None
    password: str | None = None
    passphrase: str | None = None
    max_running: int = Field(default=8, ge=1, le=1000)
    return_agent_socket: str | None = None


class ServerOrder(Model):
    names: list[str] = Field(min_length=1, max_length=1000)


def public_job(job, settings=None):
    result = dict(job)
    spec = dict(result["spec"])
    spec["environment_variable_names"] = sorted(spec.pop("env", {}))
    result["spec"] = spec
    settings = settings or Settings()
    result["effective_agent_models"] = {
        phase: settings.model_for(phase, JobSpec.model_validate(job["spec"])) or "source-thread"
        for phase in AGENT_PHASES
    }
    result["effective_agent_efforts"] = {
        phase: settings.effort_for(phase, JobSpec.model_validate(job["spec"])) or "source-thread"
        for phase in AGENT_PHASES
    }
    result["launch_max_retries"] = (
        spec["launch_max_retries"]
        if spec.get("launch_max_retries") is not None
        else settings.launch_max_retries
    )
    source = spec.get("source_thread_id")
    if source:
        result["source_link"] = settings.link_template.format(thread_id=source)
    return result


def public_server(row):
    return {
        **row,
        "config": {
            key: value
            for key, value in row["config"].items()
            if key not in ("password_ref", "passphrase_ref", "key_file")
        },
    }


def create_app(home: Path):
    from .cli import daemon_state, execute, parser

    initial_settings = load_settings(home)
    access = Access(home)
    if initial_settings.public_url and not access.has_admin():
        raise ValueError("Configure an administrator token before enabling public access")
    db = Database(home)
    if db.schema_version() != SCHEMA_VERSION and daemon_state(home)["running"]:
        raise ValueError("Stop the daemon before upgrading the database, then restart it")
    db.ensure_schema()
    gateway = CodexGateway(home, db)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await gateway.close()

    app = FastAPI(title="DeepQueue", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.codex = gateway
    password_throttle = PasswordThrottle()
    app.state.password_throttle = password_throttle
    app.include_router(codex_router(gateway))

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        current = load_settings(home)
        allowed_hosts = {"localhost", "127.0.0.1", "::1"}
        if current.public_url:
            allowed_hosts.add(urlsplit(current.public_url).hostname)
        if request.url.hostname not in allowed_hosts:
            return JSONResponse({"detail": "Unrecognized queue host"}, 400)
        if request.url.path.startswith("/api/") and request.method != "GET":
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if request.headers.get("x-deepqueue") != "1" or (
                origin and origin not in {expected, current.public_url}
            ):
                return JSONResponse({"detail": "A same-origin DeepQueue request is required"}, 403)
        request.state.principal = None
        if request.url.path.startswith("/api/") and request.url.path not in (
            "/api/auth",
            "/api/auth/login",
        ):
            if access.enabled() or current.public_url:
                authorization = request.headers.get("authorization", "")
                principal = (
                    access.authenticate(authorization[7:])
                    if authorization.startswith("Bearer ")
                    else access.authenticate_session(request.cookies.get(COOKIE))
                )
                if principal is None:
                    return JSONResponse({"detail": "请先登录或提供有效访问令牌"}, 401)
                request.state.principal = principal
                if principal["server"] is not None and not (
                    request.url.path.startswith(("/api/jobs", "/api/batch"))
                    or request.url.path in ("/api/models", "/api/identity", "/api/servers")
                    and request.method == "GET"
                ):
                    return JSONResponse(
                        {"detail": "A server token cannot administer the queue"}, 403
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def bound_server(request):
        principal = request.state.principal
        return principal["server"] if principal else None

    def require_server(request, name):
        if bound_server(request) not in (None, name):
            raise HTTPException(403, "This token is bound to a different execution server")

    def checked_job(request, job_id):
        job = db.job(job_id)
        require_server(request, job["server"])
        return job

    def checked_batch(request, name, server=None):
        if server is not None:
            require_server(request, server)
        result = db.batch(name, server)
        for job in result["experiments"]:
            require_server(request, job["server"])
        return result

    def checked_manifest(request, manifest, server=None):
        if server is not None:
            require_server(request, server)
        if bound_server(request) or server is not None:
            from .batches import compile_batch

            for spec in compile_batch(manifest).experiments.values():
                require_server(request, spec.server)
                if server is not None and spec.server != server:
                    raise HTTPException(422, "All experiments must use the workspace server")
        return manifest

    def set_session(response, request, principal, *, remember=True):
        response.set_cookie(
            COOKIE,
            access.session(principal, remember=remember),
            max_age=REMEMBER_SECONDS if remember else None,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )

    @app.get("/api/auth")
    def authentication():
        return {
            "required": access.enabled() or bool(load_settings(home).public_url),
            "password_enabled": access.password_status()["enabled"],
        }

    @app.post("/api/auth/login")
    def login(data: Login, request: Request):
        if data.password is not None:
            with password_throttle.attempt(request.client.host if request.client else "unknown"):
                principal = access.authenticate_password(data.password.get_secret_value())
        else:
            principal = access.authenticate(data.token.get_secret_value())
        if principal is None or principal["server"] is not None:
            raise HTTPException(
                401, "管理员密码无效或未启用" if data.password is not None else "管理员访问令牌无效"
            )
        response = JSONResponse({"ok": True})
        set_session(response, request, principal, remember=data.remember)
        return response

    @app.post("/api/auth/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    def password_settings(principal):
        return {
            **access.password_status(),
            "requires_current_password": bool(principal and principal.get("method") == "password"),
        }

    @app.get("/api/auth/password")
    def get_password(request: Request):
        return password_settings(request.state.principal)

    @app.post("/api/auth/password")
    def save_password(data: PasswordSettings, request: Request):
        principal = request.state.principal
        if principal is None or principal["server"] is not None:
            raise HTTPException(403, "请先使用管理员令牌登录")
        expected_id = (access.read().get("password") or {}).get("id")
        password_session = principal.get("method") == "password"
        with password_throttle.attempt(request.client.host if request.client else "unknown"):
            if password_session:
                current = access.authenticate_password(
                    data.current_password.get_secret_value() if data.current_password else None
                )
                if not current or current["id"] != principal["id"]:
                    raise HTTPException(401, "当前密码不正确，请重新输入")
            if data.enabled:
                updated = access.set_password(
                    data.password.get_secret_value(), expected_id=expected_id
                )
            else:
                access.disable_password(expected_id=expected_id)
                updated = None
        response = JSONResponse(
            {
                **password_settings(updated if password_session else principal),
                "login_required": password_session and not data.enabled,
            }
        )
        if password_session:
            if updated:
                set_session(
                    response,
                    request,
                    updated,
                    remember=access.session_remembered(request.cookies.get(COOKIE)),
                )
            else:
                response.delete_cookie(COOKIE)
        return response

    @app.exception_handler(LoginThrottled)
    def throttled(request, exc):
        return JSONResponse(
            {"detail": str(exc)}, 429, headers={"Retry-After": str(exc.retry_after)}
        )

    @app.exception_handler(RequestValidationError)
    def invalid_body(request, exc):
        # Validation must never echo login passwords, SSH credentials or tokens.
        return JSONResponse(
            {
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                    for item in exc.errors()
                ]
            },
            422,
        )

    @app.get("/api/identity")
    def identity(request: Request):
        return {"server": bound_server(request), "public_url": load_settings(home).public_url}

    @app.get("/api/deployment")
    def deployment():
        return {"public_url": load_settings(home).public_url, "auth_enabled": access.enabled()}

    @app.post("/api/deployment")
    def save_deployment(data: DeploymentSettings, request: Request):
        url = service_url(data.public_url)
        credential = access.create("Administrator") if url and not access.has_admin() else None
        updated = update_settings(home, {"public_url": url})
        response = JSONResponse(
            {
                "public_url": updated.public_url,
                "auth_enabled": access.enabled(),
                "credential": credential,
            }
        )
        if credential:
            set_session(response, request, credential)
        return response

    @app.get("/api/access")
    def list_access():
        return access.list()

    @app.post("/api/access/{token_id}/revoke")
    def revoke_access(token_id: str):
        access.revoke(token_id)
        return {"ok": True}

    @app.post("/api/servers/{name}/access")
    def create_access(name: str, data: AccessRequest):
        db.server(name)
        if not access.has_admin():
            raise ValueError("Set the public queue address and administrator access first")
        return access.create(data.label, name)

    @app.get("/api/servers")
    def list_servers(request: Request):
        return [
            public_server(row)
            for row in db.servers()
            if bound_server(request) in (None, row["name"])
        ]

    @app.get("/api/servers/{name}/skill")
    def server_skill(name: str):
        from .skills import skill_archive

        db.server(name)
        url = load_settings(home).public_url
        if not url:
            raise ValueError("Set the public queue address in General Settings first")
        return Response(
            skill_archive(url, name),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="deepqueue-{name}-skill.zip"'},
        )

    @app.post("/api/servers/{name}/callback")
    def configure_callback(name: str, data: CallbackSettings):
        db.set_server_callback(name, data.return_agent_socket)
        return public_server(db.server(name))

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(ValidationError)
    async def invalid_model(request, exc):
        return JSONResponse({"detail": str(exc)}, 422)

    @app.get("/api/state")
    def state(server: str | None = None):
        jobs = db.jobs(server=server, limit=10000)
        current_settings = load_settings(home)
        order = Scheduler(home, current_settings).queue_order(
            [job for job in jobs if job["status"] in PRESTART and not job["pause_sources"]]
        )
        positions = {}
        server_counts = {}
        for job in order:
            server_counts[job["server"]] = server_counts.get(job["server"], 0) + 1
            positions[job["id"]] = server_counts[job["server"]]
        return {
            "scope_server": server,
            "home": str(home),
            "public_url": current_settings.public_url,
            "auth_enabled": access.enabled(),
            "cwd": str(Path.cwd()),
            "model": current_settings.model,
            "agent_models": current_settings.agent_models.model_dump(exclude_none=True),
            "agent_effort": current_settings.agent_effort,
            "agent_efforts": current_settings.agent_efforts.model_dump(exclude_none=True),
            "launch_max_retries": current_settings.launch_max_retries,
            "daemon": daemon_state(home),
            "servers": [
                {
                    **public_server(row),
                    "reservations": [
                        {
                            "job_id": run["job_id"],
                            "allocation": run["allocation"],
                            "holding": run["finished_at"] is not None,
                        }
                        for run in db.active_runs(row["name"])
                    ],
                    "snapshot_stale": (
                        time.time() - (row["snapshot"] or {}).get("received_at", 0)
                        > current_settings.snapshot_max_age_seconds
                    ),
                }
                for row in db.servers()
            ],
            "batches": db.batches(server),
            "jobs": [
                {
                    **{
                        key: job[key]
                        for key in (
                            "id",
                            "server",
                            "status",
                            "priority",
                            "created_at",
                            "updated_at",
                            "paused",
                            "pause_sources",
                            "batch_name",
                            "reason",
                            "resources",
                            "archive_status",
                            "run_id",
                            "completion_mode",
                            "launch_status",
                            "launch_retries",
                        )
                    },
                    "title": job["spec"]["title"],
                    "command": job["spec"]["command"],
                    "parameters": job["spec"].get("parameters", {}),
                    "source_thread_id": job["spec"].get("source_thread_id"),
                    "parent_job_id": job["spec"].get("parent_job_id"),
                    "improvement_round": job["spec"].get("improvement_round", 0),
                    "position": positions.get(job["id"]),
                }
                for job in jobs
            ],
            "truncated": len(jobs) == 10000,
        }

    @app.get("/api/settings")
    def settings():
        return load_settings(home).model_dump(include=AGENT_SETTING_FIELDS)

    @app.post("/api/settings")
    def save_settings(settings: GeneralSettings):
        updated = update_settings(home, settings.model_dump(exclude_unset=True))
        return updated.model_dump(include=AGENT_SETTING_FIELDS)

    @app.get("/api/models")
    async def models(request: Request, server: str | None = None):
        current = load_settings(home)
        target = server or bound_server(request)
        source_server = None
        if target:
            require_server(request, target)
            source_server = Server.model_validate(db.server(target)["config"])
            callback = source_server.return_agent_socket
            if callback:
                current = current.model_copy(update={"return_agent_socket": callback})
        connection = (
            {"home": home, "server": source_server}
            if (
                source_server
                and source_server.codex.enabled
                and not source_server.return_agent_socket
            )
            else {}
        )
        return await model_catalog(current, **connection)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, request: Request):
        checked_job(request, job_id)
        return public_job(db.detail(job_id), load_settings(home))

    @app.get("/api/jobs")
    def list_jobs(
        request: Request,
        server: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=10000),
    ):
        if server:
            require_server(request, server)
        return [
            public_job(job, load_settings(home))
            for job in db.jobs([status] if status else None, server or bound_server(request), limit)
        ]

    @app.get("/api/jobs/{job_id}/links")
    def job_links(job_id: str, request: Request):
        checked_job(request, job_id)
        return execute(parser().parse_args(["--home", str(home), "job", "links", job_id]))

    @app.post("/api/jobs")
    def submit_job(spec: JobSpec, request: Request, server: str | None = None):
        if server is not None:
            require_server(request, server)
            if spec.server != server:
                raise HTTPException(422, "The experiment must use the workspace server")
        require_server(request, spec.server)
        return public_job(db.submit(spec), load_settings(home))

    @app.post("/api/jobs/{job_id}/control")
    def control_job(job_id: str, control: Control, request: Request):
        checked_job(request, job_id)
        if control.action in ("pause", "resume"):
            db.set_paused(job_id, control.action == "pause")
        elif control.action == "cancel":
            db.cancel(job_id)
        elif control.action == "priority" and control.priority is not None:
            db.set_priority(job_id, control.priority)
        elif control.action == "approve":
            db.approve(job_id, control.resources)
        elif control.action == "retry-agent" and control.phase:
            db.retry_agent(job_id, control.phase)
        elif control.action == "mode" and control.completion_mode:
            db.set_completion_mode(job_id, control.completion_mode, control.max_improvement_rounds)
        else:
            raise ValueError("This control requires priority or phase")
        return public_job(db.detail(job_id), load_settings(home))

    @app.post("/api/jobs/{job_id}/settings")
    def update_job(job_id: str, update: JobUpdate, request: Request):
        checked_job(request, job_id)
        db.update_job(job_id, update)
        return public_job(db.detail(job_id), load_settings(home))

    @app.get("/api/jobs/{job_id}/logs")
    def logs(
        job_id: str,
        request: Request,
        size: int = Query(default=65536, ge=1, le=262144),
        run_id: str | None = None,
    ):
        job = checked_job(request, job_id)
        selected = checked_run(job, run_id)
        if not selected:
            return {"text": "", "bytes": 0, "truncated": False}
        server = Server.model_validate(db.server(job["server"])["config"])
        return Transport(home, server).call("logs", run_id=selected, limit=size)

    def checked_run(job, run_id):
        if run_id and db.run(run_id)["job_id"] != job["id"]:
            raise HTTPException(status_code=404, detail="Run does not belong to this job")
        return run_id or job["run_id"]

    @app.get("/api/jobs/{job_id}/terminal")
    def terminal(job_id: str, request: Request, run_id: str | None = None):
        job = checked_job(request, job_id)
        selected = checked_run(job, run_id)
        if not selected:
            return {"available": False}
        server = Server.model_validate(db.server(job["server"])["config"])
        return Transport(home, server).terminal(selected)

    @app.get("/api/jobs/{job_id}/artifacts")
    def artifacts(job_id: str, request: Request):
        job = checked_job(request, job_id)
        if not job["run_id"]:
            return {"files": []}
        server = Server.model_validate(db.server(job["server"])["config"])
        return Transport(home, server).call(
            "evidence", cwd=job["spec"]["cwd"], paths=job["spec"]["artifact_files"]
        )

    @app.get("/api/jobs/{job_id}/runtime")
    def runtime(job_id: str, request: Request):
        job = checked_job(request, job_id)
        if not job["run_id"]:
            return None
        server = Server.model_validate(db.server(job["server"])["config"])
        return Transport(home, server).call("inspect", run_id=job["run_id"])

    @app.post("/api/batches/preview")
    def preview_batch(manifest: dict, request: Request, server: str | None = None):
        return db.preview_batch(checked_manifest(request, manifest, server))

    @app.post("/api/batches")
    def submit_batch(manifest: dict, request: Request, server: str | None = None):
        return db.submit_batch(checked_manifest(request, manifest, server))

    @app.get("/api/batches")
    def list_batches(request: Request, server: str | None = None):
        if server is not None:
            require_server(request, server)
        rows = db.batches(server)
        if server is not None:
            return rows
        if bound_server(request):
            rows = [
                row
                for row in rows
                if all(
                    job["server"] == bound_server(request)
                    for job in db.batch(row["name"])["experiments"]
                )
            ]
        return rows

    @app.get("/api/batch")
    def batch(name: str, request: Request, server: str | None = None):
        result = checked_batch(request, name, server)
        result["experiments"] = [
            public_job(job, load_settings(home)) for job in result["experiments"]
        ]
        return result

    @app.get("/api/batch/report")
    def batch_report(name: str, request: Request, server: str | None = None):
        checked_batch(request, name, server)
        return execute(
            parser().parse_args(
                ["--home", str(home), "batch", "report", name]
                + (["--server", server] if server is not None else [])
            )
        )

    @app.post("/api/batch/control")
    def control_batch(name: str, control: Control, request: Request, server: str | None = None):
        checked_batch(request, name, server)
        if control.action in ("pause", "resume"):
            db.set_batch_paused(name, control.action == "pause", server)
        elif control.action == "cancel":
            db.cancel_batch(name, server)
        elif control.action == "priority" and control.priority is not None:
            return db.set_batch_priority(name, control.priority, server)
        else:
            raise ValueError("Unsupported batch control")
        return {"ok": True}

    @app.post("/api/daemon")
    def control_daemon(control: DaemonControl):
        return execute(parser().parse_args(["--home", str(home), "daemon", control.action]))

    @app.post("/api/servers/{name}/control")
    def control_server(name: str, control: Control):
        if control.action not in ("pause", "resume"):
            raise ValueError("Unsupported server control")
        db.enable_server(name, control.action == "resume")
        return public_server(db.server(name))

    @app.post("/api/servers/order")
    def order_servers(data: ServerOrder):
        db.reorder_servers(data.names)
        return [public_server(row) for row in db.servers()]

    @app.post("/api/servers/{name}/settings")
    async def edit_server(name: str, data: ServerUpdate):
        import asyncio

        row = await asyncio.to_thread(update_server, home, name, data)
        # Apply connection changes to the existing gateway on the same event loop.
        await gateway.session(name)
        return public_server(row)

    @app.post("/api/servers/{name}/probe")
    def probe_server(name: str):
        return execute(parser().parse_args(["--home", str(home), "server", "probe", name]))

    @app.get("/api/ssh/fingerprint")
    def ssh_fingerprint(
        host: str = Query(min_length=1, max_length=253),
        port: int = Query(default=22, ge=1, le=65535),
    ):
        key = host_key(host, port)
        return {"fingerprint": fingerprint(key), "key_type": key.get_name()}

    @app.post("/api/servers")
    def add_server(data: RemoteServer):
        if data.password and data.key_file:
            raise ValueError("Choose password authentication or a key file")
        server = Server(
            name=data.name,
            display_name=data.display_name,
            kind="ssh",
            host=data.host,
            port=data.port,
            username=data.username,
            key_file=data.key_file,
            max_running=data.max_running,
            return_agent_socket=data.return_agent_socket,
        )
        if any(row["name"] == data.name for row in db.servers()):
            raise ValueError(f"Server already exists: {data.name}")
        trust_host(home, data.host, data.port, data.fingerprint)
        vault = Secrets(home)
        server.password_ref = vault.put(data.password) if data.password else None
        server.passphrase_ref = vault.put(data.passphrase) if data.passphrase else None
        db.add_server(server)
        return public_server(db.server(data.name))

    static = Path(__file__).parent / "web_static"
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (static / "index.html").exists():
            raise HTTPException(503, "Build the frontend first: npm --prefix frontend run build")
        return FileResponse(static / "index.html")

    return app
