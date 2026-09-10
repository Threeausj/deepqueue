import io
import json
import socket
import threading
import time
import zipfile
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from deepqueue.access import COOKIE, Access
from deepqueue.cli import execute, parser
from deepqueue.config import update_settings
from deepqueue.models import JobSpec, Resources, Server, Settings, service_url
from deepqueue.remote import Client, client_config
from deepqueue.scheduler import Scheduler
from deepqueue.web import create_app


def add_servers(db):
    for name in ("gpu-a", "gpu-b"):
        db.add_server(Server(name=name, worker_root=str(db.home / name)))


def headers(credential):
    return {"Authorization": "Bearer " + credential["token"], "X-DeepQueue": "1"}


def spec(db, server, **updates):
    return JobSpec(
        server=server,
        cwd=str(db.home),
        command="true",
        resources=Resources(ram_mib=128),
        skip_estimate=True,
        **updates,
    )


def test_public_auth_login_rotation_and_origin_boundary(db):
    access = Access(db.home)
    admin = access.create("Initial administrator")
    update_settings(db.home, {"public_url": "https://queue.example.test"})
    with TestClient(create_app(db.home), base_url="https://queue.example.test") as web:
        assert web.get("/api/state").status_code == 401
        assert web.get("/api/auth").json() == {"required": True}
        assert web.get("/api/state", headers={"Host": "foreign.example"}).status_code == 400
        assert web.post("/api/auth/login", json={"token": admin["token"]}).status_code == 403
        bad_origin = {"X-DeepQueue": "1", "Origin": "https://foreign.example"}
        assert (
            web.post(
                "/api/auth/login", headers=bad_origin, json={"token": admin["token"]}
            ).status_code
            == 403
        )
        login = web.post(
            "/api/auth/login", headers={"X-DeepQueue": "1"}, json={"token": admin["token"]}
        )
        assert login.status_code == 200
        assert all(
            flag in login.headers["set-cookie"].lower()
            for flag in ("httponly", "secure", "samesite=strict")
        )
        assert web.get("/api/state").status_code == 200
        saved_session = web.cookies.get(COOKIE)
        with pytest.raises(ValueError, match="replacement"):
            access.revoke(admin["id"])
        replacement = access.create("Replacement administrator")
        access.revoke(admin["id"])
        assert access.authenticate_session(saved_session) is None
        assert web.get("/api/state").status_code == 401
        assert web.get("/api/state", headers=headers(replacement)).status_code == 200
        serialized = json.dumps(access.list())
        assert replacement["token"] not in serialized and "digest" not in serialized
        assert replacement["token"] not in access.path.read_text()
        assert access.path.stat().st_mode & 0o777 == 0o600


def test_server_tokens_reject_cross_server_jobs_batches_and_admin_routes(db):
    add_servers(db)
    access = Access(db.home)
    access.create("Admin")
    token_a = access.create("Server A", "gpu-a")
    token_b = access.create("Server B", "gpu-b")
    b_job = db.submit(spec(db, "gpu-b"))
    b_batch = {
        "name": "gpu-b-round",
        "defaults": spec(db, "gpu-b").model_dump(),
        "experiments": [{"key": "one"}],
    }
    db.submit_batch(b_batch)
    with TestClient(
        create_app(db.home), base_url="http://localhost", headers=headers(token_a)
    ) as web:
        assert web.get("/api/identity").json()["server"] == "gpu-a"
        assert [row["name"] for row in web.get("/api/servers").json()] == ["gpu-a"]
        assert web.get("/api/jobs", params={"server": "gpu-b"}).status_code == 403
        assert web.get("/api/jobs").json() == []
        for path in ("/api/state", "/api/access", "/api/settings", "/api/deployment"):
            assert web.get(path).status_code == 403
            assert web.post(path, json={}).status_code == 403
        assert web.post("/api/servers/gpu-a/access", json={}).status_code == 403
        assert web.post("/api/servers/gpu-a/callback", json={}).status_code == 403
        assert web.post("/api/jobs", json=spec(db, "gpu-b").model_dump()).status_code == 403
        for suffix in ("", "/links", "/logs", "/terminal", "/artifacts", "/runtime"):
            assert web.get(f"/api/jobs/{b_job['id']}{suffix}").status_code == 403
        assert (
            web.post(f"/api/jobs/{b_job['id']}/control", json={"action": "cancel"}).status_code
            == 403
        )
        for path in ("/api/batch", "/api/batch/report"):
            assert web.get(path, params={"name": b_batch["name"]}).status_code == 403
        assert (
            web.post(
                "/api/batch/control", params={"name": b_batch["name"]}, json={"action": "cancel"}
            ).status_code
            == 403
        )
        assert web.get("/api/batches").json() == []
        before = len(db.jobs())
        mixed = {
            "name": "mixed",
            "defaults": spec(db, "gpu-a").model_dump(),
            "experiments": [{"key": "own"}, {"key": "other", "server": "gpu-b"}],
        }
        for path in ("/api/batches/preview", "/api/batches"):
            assert web.post(path, json=mixed).status_code == 403
        assert len(db.jobs()) == before
        created = web.post("/api/jobs", json=spec(db, "gpu-a").model_dump()).json()
        assert created["server"] == "gpu-a"
        assert [job["id"] for job in web.get("/api/jobs").json()] == [created["id"]]
        assert web.get(f"/api/jobs/{created['id']}", headers=headers(token_b)).status_code == 403
        access.revoke(token_a["id"])
        assert web.get("/api/jobs").status_code == 401
    assert db.job(b_job["id"])["status"] == "queued"


def test_public_setup_generates_admin_and_server_skill_without_secrets(db):
    add_servers(db)
    with TestClient(
        create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"}
    ) as web:
        assert (
            web.post(
                "/api/deployment", json={"public_url": "http://queue.example.test"}
            ).status_code
            == 400
        )
        assert not Access(db.home).enabled()
        result = web.post("/api/deployment", json={"public_url": "https://queue.example.test/"})
        assert result.status_code == 200
        assert result.json()["credential"]["token"]
        assert web.get("/api/state").status_code == 200
        token = web.post("/api/servers/gpu-a/access", json={"label": "GPU A skill"}).json()
        archive = web.get("/api/servers/gpu-a/skill")
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as files:
            profile = json.loads(files.read("deepqueue/deployment.json"))
            assert profile == {"url": "https://queue.example.test", "server": "gpu-a"}
            text = files.read("deepqueue/SKILL.md").decode()
            assert "https://queue.example.test" in text and "--target-server gpu-a" in text
            assert "deepqueue/references/remote.md" in files.namelist()
            assert token["token"] not in "".join(
                files.read(name).decode() for name in files.namelist()
            )
        assert (
            web.post(
                "/api/servers/gpu-a/callback",
                json={"return_agent_socket": "/run/deepqueue/gpu-a.sock"},
            ).status_code
            == 200
        )
        assert db.server("gpu-a")["config"]["return_agent_socket"] == "/run/deepqueue/gpu-a.sock"
        assert (
            web.post(
                "/api/servers/gpu-a/callback", json={"return_agent_socket": "relative/path"}
            ).status_code
            == 422
        )
        assert web.post("/api/auth/logout", json={}).status_code == 200
        assert web.get("/api/state").status_code == 401


@contextmanager
def live_http(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def test_remote_cli_submits_and_controls_without_a_local_queue(db, tmp_path, monkeypatch):
    add_servers(db)
    access = Access(db.home)
    access.create("Admin")
    credential = access.create("Server A", "gpu-a")
    profile = tmp_path / "client.json"
    monkeypatch.setenv("DEEPQUEUE_CLIENT_CONFIG", str(profile))
    monkeypatch.setenv("FIXTURE_TOKEN", credential["token"])
    monkeypatch.setenv("CODEX_THREAD_ID", "actual-submitting-fixture")
    manifest = tmp_path / "batch.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "gpu-a-remote-test",
                "defaults": {
                    "cwd": str(db.home),
                    "command": "true",
                    "resources": {"ram_mib": 128},
                    "skip_estimate": True,
                },
                "experiments": [{"key": "first"}, {"key": "second", "depends_on": ["first"]}],
            }
        )
    )
    with live_http(create_app(db.home)) as url:
        configured = execute(
            parser().parse_args(
                [
                    "--url",
                    url,
                    "client",
                    "configure",
                    "--token-env",
                    "FIXTURE_TOKEN",
                ]
            )
        )
        assert configured["server"] == "gpu-a"
        assert credential["token"] not in profile.read_text()
        assert profile.stat().st_mode & 0o777 == 0o600
        execute(parser().parse_args(["batch", "preview", str(manifest), "--from-agent"]))
        assert not db.jobs()
        submitted = execute(parser().parse_args(["batch", "submit", str(manifest), "--from-agent"]))
        assert execute(parser().parse_args(["batch", "submit", str(manifest), "--from-agent"]))[
            "existing"
        ]
        jobs = execute(parser().parse_args(["job", "list"]))
        assert len(jobs) == 2
        assert all(job["server"] == "gpu-a" for job in jobs)
        assert all(job["spec"]["source_thread_id"] == "actual-submitting-fixture" for job in jobs)
        first = submitted["jobs"]["first"]
        execute(parser().parse_args(["job", "pause", first]))
        assert db.job(first)["paused"]
        execute(parser().parse_args(["job", "priority", first, "50"]))
        assert db.job(first)["priority"] == 50
        assert (
            execute(parser().parse_args(["job", "links", first]))[0]["thread_id"]
            == "actual-submitting-fixture"
        )
        assert (
            len(execute(parser().parse_args(["batch", "report", submitted["name"]]))["experiments"])
            == 2
        )
        with pytest.raises(ValueError, match="another server"):
            execute(
                parser().parse_args(
                    [
                        "job",
                        "submit",
                        "--server",
                        "gpu-b",
                        "--command",
                        "true",
                        "--from-agent",
                    ]
                )
            )
        with pytest.raises(ValueError, match="remote submission client"):
            execute(parser().parse_args(["init"]))
        assert client_config(parser().parse_args(["--home", str(db.home), "job", "list"])) is None
        other = client_config(
            parser().parse_args(["--url", "https://other.example", "job", "list"])
        )
        assert other["token"] is None and other["server"] is None


def test_client_does_not_forward_tokens_on_redirect(db):
    credential = Access(db.home).create("Admin")
    app = create_app(db.home)

    @app.get("/api/redirect")
    def redirect():
        return RedirectResponse("https://not-the-queue.invalid/api/state", status_code=302)

    with live_http(app) as url:
        with pytest.raises(ValueError, match="HTTP 302"):
            Client({"url": url, "token": credential["token"]}).request("/redirect")


async def test_busy_server_keeps_jobs_while_other_server_allocates_its_own_gpus(db):
    add_servers(db)
    blocked = db.submit(
        spec(db, "gpu-a", priority=100).model_copy(
            update={
                "resources": Resources(gpu_count=1, ram_mib=128),
            }
        )
    )
    runnable = db.submit(
        spec(db, "gpu-b").model_copy(
            update={
                "resources": Resources(gpu_count=1, ram_mib=128),
            }
        )
    )
    calls = []

    class Inventory:
        def __init__(self, home, server):
            self.server = server

        def call(self, action, **params):
            calls.append((self.server.name, action))
            assert action == "probe"
            return {
                "cpu_count": 16,
                "cpu_available": 16,
                "ram_available_mib": 64000,
                "gpus": [
                    {
                        "index": 0,
                        "uuid": f"GPU-{self.server.name}",
                        "name": "fixture",
                        "memory_total_mib": 16000,
                        "memory_free_mib": 16000,
                        "processes": [],
                        "utilization": 100 if self.server.name == "gpu-a" else 0,
                    }
                ],
            }

    scheduler = Scheduler(db.home, Settings(), transport_factory=Inventory)
    for name in ("gpu-a", "gpu-b"):
        await scheduler.host_tick(Server.model_validate(db.server(name)["config"]))
    assert db.job(blocked["id"])["status"] == "queued"
    assert db.job(blocked["id"])["run_id"] is None
    assert db.job(runnable["id"])["status"] == "starting"
    run = db.run(db.job(runnable["id"])["run_id"])
    assert run["server"] == "gpu-b" and run["allocation"][0]["uuid"] == "GPU-gpu-b"
    assert not db.leases("gpu-a")
    assert calls == [("gpu-a", "probe"), ("gpu-b", "probe")]


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://user:password@queue.example",
        "https://queue.example/path",
        "https://queue.example?token=secret",
        "https://queue.example:0",
        "https://queue.example:99999",
        "https://queue.example\n",
        "https://[::1%$(id)]",
    ],
)
def test_invalid_public_urls_are_rejected(url):
    with pytest.raises(ValueError):
        service_url(url)
