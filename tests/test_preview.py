import contextlib
import select
import socket
import threading
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from urllib.parse import urlsplit

import paramiko
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from deepqueue.access import COOKIE, Access
from deepqueue.config import Secrets, update_settings
from deepqueue.models import Server
from deepqueue.preview import PreviewManager, PreviewMiddleware, PreviewReference, PreviewTarget
from deepqueue.transport import fingerprint, trust_host
from deepqueue.web import create_app

preview_service = run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/workspace_fixture.py")
)["preview_service"]


@contextlib.contextmanager
def forwarded_server(db, port):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    host_key = paramiko.RSAKey.generate(2048)
    connections = []
    stop = threading.Event()

    class ForwardAuth(paramiko.ServerInterface):
        def __init__(self):
            self.destinations = {}

        def check_auth_password(self, username, password):
            return (
                paramiko.AUTH_SUCCESSFUL
                if (username, password) == ("fixture", "fixture-password")
                else paramiko.AUTH_FAILED
            )

        def check_channel_direct_tcpip_request(self, channel_id, origin, destination):
            if destination != ("127.0.0.1", port):
                return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            self.destinations[channel_id] = destination
            return paramiko.OPEN_SUCCEEDED

    def relay(channel, destination):
        upstream = socket.create_connection(destination, 3)
        try:
            while not stop.is_set():
                ready, _, _ = select.select([channel, upstream], [], [], 0.1)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is channel else channel).sendall(data)
        except (EOFError, OSError):
            pass
        finally:
            with contextlib.suppress(EOFError, OSError):
                channel.close()
            upstream.close()

    def serve_connection(sock):
        transport = paramiko.Transport(sock)
        connections.append(transport)
        transport.add_server_key(host_key)
        auth = ForwardAuth()
        try:
            transport.start_server(server=auth)
            while not stop.is_set() and transport.is_active():
                channel = transport.accept(0.1)
                if channel:
                    threading.Thread(
                        target=relay, args=(channel, auth.destinations[channel.chanid]), daemon=True
                    ).start()
        finally:
            transport.close()

    def accept():
        while not stop.is_set():
            try:
                sock, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=serve_connection, args=(sock,), daemon=True).start()

    worker = threading.Thread(target=accept, daemon=True)
    worker.start()
    ssh_port = listener.getsockname()[1]
    trust_host(db.home, "127.0.0.1", ssh_port, fingerprint(host_key))
    try:
        yield Server(
            name="preview-ssh",
            kind="ssh",
            host="127.0.0.1",
            port=ssh_port,
            username="fixture",
            password_ref=Secrets(db.home).put("fixture-password"),
        )
    finally:
        stop.set()
        listener.close()
        for connection in connections:
            connection.close()
        worker.join(2)


@contextlib.contextmanager
def preview_client(db, port, server=None, auth=None):
    manager = PreviewManager(SimpleNamespace(home=db.home))

    async def thread(thread_id):
        return {"id": thread_id, "cwd": "/tmp/fixture"}

    session = SimpleNamespace(
        state="connected",
        generation="connection-1",
        server=server or Server(name="local"),
        tools=SimpleNamespace(thread=thread),
        require_client=lambda: None,
    )
    app = FastAPI()
    app.add_middleware(PreviewMiddleware, manager=manager)

    @app.post("/open")
    async def open_preview(request: Request):
        return await manager.create(session, "task", PreviewTarget(port=port), request)

    @app.post("/close")
    async def close_preview(data: PreviewReference):
        preview = manager.checked(data, session, "task")
        manager.previews.pop(data.id)
        await preview.close()
        return {"ok": True}

    @app.post("/logout")
    async def logout():
        await manager.close_auth(auth or ("session", None))
        return {"ok": True}

    headers = {"Authorization": "Bearer " + auth[1]} if auth and auth[0] == "token" else {}
    if auth and auth[0] == "session":
        headers["Cookie"] = COOKIE + "=" + auth[1]
    with TestClient(app, base_url="http://localhost", headers=headers) as client:
        try:
            yield client, manager, session
        finally:
            client.portal.call(manager.close)


@pytest.mark.parametrize("ssh", [False, True])
def test_project_web_preview_full_origin_http_and_websocket(db, ssh):
    with preview_service() as port:
        with forwarded_server(db, port) if ssh else contextlib.nullcontext(None) as server:
            with preview_client(db, port, server) as (client, manager, session):
                response = client.post("/open")
                assert response.status_code == 200, response.text
                preview = response.json()
                origin = preview["url"].rstrip("/")
                assert urlsplit(origin).hostname != "localhost"
                assert "实时项目服务" in client.get(origin + "/").text
                assert (
                    client.get(origin + "/assets/app.css")
                    .headers["content-type"]
                    .startswith("text/css")
                )
                assert client.get(origin + "/api/metrics").json() == {"accuracy": 0.93}
                echoed = client.post(
                    origin + "/api/echo?q=%E7%B2%BE%E5%BA%A6",
                    content="test body",
                    headers={
                        "Cookie": "deepqueue_session=secret; project_session=ok",
                        "Origin": origin,
                        "Referer": origin + "/nested",
                        "X-DeepQueue": "1",
                        "Authorization": "Bearer dq_fixture_admin",
                    },
                ).json()
                assert echoed["body"] == "test body"
                assert "deepqueue" not in echoed["headers"]["cookie"]
                assert echoed["headers"]["cookie"] == "project_session=ok"
                assert echoed["headers"]["origin"] == f"http://127.0.0.1:{port}"
                assert "x-deepqueue" not in echoed["headers"]
                assert "authorization" not in echoed["headers"]
                assert echoed["query"] == "q=%E7%B2%BE%E5%BA%A6"
                redirect = client.get(origin + "/redirect", follow_redirects=False)
                assert redirect.headers["location"] == origin + "/api/metrics?from=redirect"
                cookies = client.get(origin + "/cookies").headers.get_list("set-cookie")
                assert len(cookies) == 1 and "domain=" not in cookies[0].lower()
                with client.websocket_connect(origin.replace("http:", "ws:") + "/socket") as ws:
                    ws.send_text("SSH preview" if ssh else "local preview")
                    assert ws.receive_text().startswith("WebSocket: ")
                    ws.send_bytes(b"\x00\x01binary")
                    assert ws.receive_bytes() == b"\x00\x01binary"
                assert (
                    client.get(origin.replace(".localhost", ".wrong.test") + "/").status_code == 404
                )
                assert client.post("/close", json={"id": preview["id"]}).status_code == 200
                assert client.get(origin + "/api/metrics").status_code == 404
                assert not manager.previews


def test_preview_expires_on_logout_revocation_and_reconnection(db):
    access = Access(db.home)
    first = access.create("first admin")
    access.create("second admin")
    cookie = access.session(access.authenticate(first["token"]), remember=True)
    with preview_service() as port:
        with preview_client(db, port, auth=("session", cookie)) as (client, manager, session):
            preview = client.post("/open").json()
            assert client.get(preview["url"]).status_code == 200
            client.post("/logout")
            assert client.get(preview["url"]).status_code == 404
            preview = client.post("/open").json()
            session.generation = "connection-2"
            assert client.get(preview["url"]).status_code == 401
            assert not manager.previews[preview["id"]].authorized()
            preview = client.post("/open").json()
            access.revoke(first["id"])
            assert client.get(preview["url"]).status_code == 401


def test_preview_origin_validation_does_not_create_credentials_or_change_public_setup(db):
    with TestClient(
        create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"}
    ) as client:
        response = client.post(
            "/api/deployment",
            json={
                "public_url": "https://queue.example.test",
                "preview_origin": "https://127.0.0.1",
            },
        )
        assert response.status_code == 422
        assert not Access(db.home).has_admin()
        assert (
            client.post(
                "/api/deployment", json={"preview_origin": "https://preview.example.test"}
            ).status_code
            == 200
        )
        assert (
            client.get("/api/deployment").json()["preview_origin"] == "https://preview.example.test"
        )
        assert not Access(db.home).has_admin()


def test_network_clients_require_explicit_preview_origin(db):
    with preview_service() as port:
        with preview_client(db, port) as (client, manager, session):
            assert client.post("http://192.168.1.2/open").status_code == 409
            update_settings(db.home, {"preview_origin": "https://preview.example.test"})
            preview = client.post("http://192.168.1.2/open").json()
            assert urlsplit(preview["url"]).hostname.endswith(".preview.example.test")
            assert client.get(preview["url"]).status_code == 200
