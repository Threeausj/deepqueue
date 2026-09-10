import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from deepqueue.access import COOKIE, REMEMBER_SECONDS, Access
from deepqueue.cli import daemon_state
from deepqueue.config import load_settings, update_settings
from deepqueue.container import ownership, setup
from deepqueue.db import Database


def test_bootstrap_is_private_idempotent_and_preserves_operator_settings(tmp_path, monkeypatch):
    home = tmp_path / "queue"
    monkeypatch.setenv("DEEPQUEUE_PUBLIC_URL", "https://queue.example.test")
    result = setup(home)
    token = result["admin"]["token"]
    assert Access(home).authenticate(token)["server"] is None
    assert token not in (home / "access.json").read_text()
    assert (home / "access.json").stat().st_mode & 0o777 == 0o600
    assert not Database(home).server("local")["config"]["enabled"]
    assert not daemon_state(home)["running"]
    update_settings(home, {"agent_effort": "high"})
    assert setup(home)["admin_created"] is False
    assert len(Access(home).list()) == 1
    assert load_settings(home).agent_effort == "high"
    with ownership(home), pytest.raises(ValueError, match="already has"):
        setup(home)


def test_bootstrap_requires_valid_public_url_before_creating_state(tmp_path, monkeypatch):
    for value in ("", "https://queue.example.test/path", "http://queue.example.test"):
        monkeypatch.setenv("DEEPQUEUE_PUBLIC_URL", value)
        with pytest.raises(ValueError):
            setup(tmp_path / "absent")
        assert not (tmp_path / "absent").exists()


def test_bootstrap_password_is_opt_in_and_preserves_changes_and_disable(tmp_path, monkeypatch):
    home = tmp_path / "queue"
    monkeypatch.setenv("DEEPQUEUE_PUBLIC_URL", "https://queue.example.test")
    monkeypatch.setenv("DEEPQUEUE_ADMIN_PASSWORD", "short")
    with pytest.raises(ValueError, match="12–128"):
        setup(home)
    assert not home.exists()
    password = "fixture-bootstrap-password"
    replacement = "fixture-changed-password"
    monkeypatch.setenv("DEEPQUEUE_ADMIN_PASSWORD", password)
    result = setup(home)
    access = Access(home)
    assert result["password_enabled"] and result["admin_created"]
    assert password not in str(result) and password not in access.path.read_text()
    assert access.authenticate_password(password)
    access.set_password(replacement)
    current = access.read()["password"]
    assert setup(home)["password_enabled"]
    assert access.read()["password"] == current
    assert access.authenticate_password(replacement)
    access.disable_password()
    assert not setup(home)["password_enabled"]
    assert not access.authenticate_password(password)


def start_container_entrypoint(home, web_port, output, auto_start=False):
    env = {
        **os.environ,
        "DEEPQUEUE_HOME": str(home),
        "DEEPQUEUE_PUBLIC_URL": "https://queue.example.test",
        "DEEPQUEUE_WEB_PORT": str(web_port),
        "DEEPQUEUE_START_SCHEDULER": str(auto_start).lower(),
        "FORWARDED_ALLOW_IPS": "*",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "deepqueue.container", "serve"],
        env=env,
        stdout=output,
        stderr=output,
    )


def wait_for(action, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = action()
            if result:
                return result
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.1)
    raise AssertionError("Container entrypoint did not reach the expected state")


@pytest.mark.parametrize("auto_start", [False, True])
def test_real_process_lifecycle_auth_web_controls_and_restart(tmp_path, monkeypatch, auto_start):
    # This is the actual entrypoint/web/scheduler in isolated state, with no jobs,
    # no enabled execution servers, no Codex login and no model calls.
    home = tmp_path / "queue"
    monkeypatch.setenv("DEEPQUEUE_PUBLIC_URL", "https://queue.example.test")
    password = "fixture-container-password"
    if auto_start:
        monkeypatch.setenv("DEEPQUEUE_ADMIN_PASSWORD", password)
    admin = setup(home)["admin"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        web_port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{web_port}"
    headers = {"Authorization": "Bearer " + admin["token"], "X-DeepQueue": "1"}
    log = tmp_path / "container.log"
    with log.open("wb") as output, httpx.Client(base_url=url, timeout=5) as client:
        process = start_container_entrypoint(home, web_port, output, auto_start)
        try:
            wait_for(lambda: client.get("/api/auth").is_success)
            assert client.get("/api/state").status_code == 401
            initial = client.get("/api/state", headers=headers).json()
            assert initial["daemon"]["running"] == auto_start
            assert initial["jobs"] == []
            assert not initial["servers"][0]["config"]["enabled"]
            # Reverse proxy HTTPS must result in a Secure login cookie.
            login = client.post(
                "/api/auth/login",
                json={"password": password} if auto_start else {"token": admin["token"]},
                headers={
                    "X-DeepQueue": "1",
                    "X-Forwarded-Proto": "https",
                    "Origin": "https://queue.example.test",
                },
            )
            assert login.is_success and "secure" in login.headers["set-cookie"].lower()
            assert f"Max-Age={REMEMBER_SECONDS}" in login.headers["set-cookie"]
            cookie = client.cookies.get(COOKIE)
            for action in ("start", "stop", "start"):
                response = client.post("/api/daemon", json={"action": action}, headers=headers)
                assert response.is_success, response.text
                assert daemon_state(home)["running"] == (action == "start")
            process.terminate()
            assert process.wait(timeout=20) == 0
            assert not daemon_state(home)["running"]
            process = start_container_entrypoint(home, web_port, output, False)
            wait_for(lambda: client.get("/api/auth").is_success)
            restored = client.get("/api/state", headers=headers)
            assert restored.is_success and not restored.json()["daemon"]["running"]
            assert client.get("/api/state", headers={"Cookie": COOKIE + "=" + cookie}).is_success
            assert len(Access(home).list()) == 1
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
    assert admin["token"] not in log.read_text()
    assert password not in log.read_text()


def test_serve_does_not_create_or_log_credentials_before_setup(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "deepqueue.container", "serve"],
        env={
            **os.environ,
            "DEEPQUEUE_HOME": str(tmp_path),
            "DEEPQUEUE_PUBLIC_URL": "https://queue.example.test",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "setup" in result.stderr
    assert not (tmp_path / "access.json").exists()
