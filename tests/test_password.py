import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from deepqueue.access import COOKIE, REMEMBER_SECONDS, SESSION_SECONDS, Access
from deepqueue.cli import execute, parser
from deepqueue.web import create_app

PASSWORD = "fixture-admin-password-123"
REPLACEMENT = "fixture-new-password-456"
HEADERS = {"X-DeepQueue": "1"}


@pytest.fixture
def access(db):
    access = Access(db.home)
    access.create("Administrator")
    access.set_password(PASSWORD)
    return access


def browser(db, **kwargs):
    return TestClient(create_app(db.home), base_url="https://localhost", headers=HEADERS, **kwargs)


def login(web, password=PASSWORD, **options):
    return web.post("/api/auth/login", json={"password": password, **options})


def test_password_is_optional_salted_private_and_does_not_replace_tokens(db):
    access = Access(db.home)
    assert not access.password_status()["enabled"]
    with pytest.raises(ValueError, match="管理员访问令牌"):
        access.set_password(PASSWORD)
    admin = access.create("Administrator")
    server = access.create("Training", "local")
    principal = access.set_password(PASSWORD)
    old = access.read()["password"]
    cookie = access.session(principal, remember=True)
    access.set_password(PASSWORD)
    current = access.read()["password"]
    assert old["salt"] != current["salt"] and old["digest"] != current["digest"]
    assert PASSWORD not in access.path.read_text()
    assert access.path.stat().st_mode & 0o777 == 0o600
    status = json.dumps(access.password_status())
    assert all(value not in status for value in (PASSWORD, current["salt"], current["digest"]))
    assert access.authenticate_password(PASSWORD)["method"] == "password"
    assert access.authenticate_password("incorrect") is None
    assert access.authenticate_session(cookie) is None
    access.disable_password()
    assert access.authenticate_password(PASSWORD) is None
    assert access.authenticate(admin["token"])["server"] is None
    assert access.authenticate(server["token"])["server"] == "local"


@pytest.mark.parametrize("password", ["short", " " * 12, "p" * 129, "p" * 12 + "\ud800"])
def test_invalid_password_preserves_previous_password(access, password):
    with pytest.raises(ValueError):
        access.set_password(password)
    assert access.authenticate_password(PASSWORD)
    assert access.authenticate_password(password) is None


@pytest.mark.parametrize("remember", [True, False])
def test_cookie_survives_new_application_and_checks_expiration_and_tampering(db, access, remember):
    before = time.time()
    with browser(db) as web:
        assert web.get("/api/state").status_code == 401
        assert web.get("/api/auth").json() == {"required": True, "password_enabled": True}
        response = login(web, remember=remember)
        assert response.status_code == 200
        header = response.headers["set-cookie"].lower()
        assert all(flag in header for flag in ("httponly", "secure", "samesite=strict"))
        if remember:
            assert f"max-age={REMEMBER_SECONDS}" in header
        else:
            assert "max-age" not in header and "expires=" not in header
        cookie = web.cookies.get(COOKIE)
        duration = REMEMBER_SECONDS if remember else SESSION_SECONDS
        assert before + duration - 1 <= int(cookie.split(".")[1]) <= time.time() + duration
        assert access.session_remembered(cookie) == remember
        assert web.get("/api/state").status_code == 200
        assert COOKIE not in json.dumps(web.get("/api/state").json())
    # Only the browser cookie is needed after both application and browser context restart.
    with browser(db) as restored:
        restored.cookies.set(COOKIE, cookie)
        assert restored.get("/api/state").status_code == 200
        assert restored.post("/api/auth/logout", json={}).status_code == 200
        # TestClient's domainless injected cookie is separate from its response cookie jar.
        restored.cookies.clear()
        assert restored.get("/api/state").status_code == 401
    assert access.authenticate_session(cookie + "x") is None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("deepqueue.access.time.time", lambda: before + duration + 10)
        assert access.authenticate_session(cookie) is None


@pytest.mark.parametrize("remember", [True, False])
def test_password_change_renews_current_browser_and_revokes_other_password_sessions(
    db, access, remember
):
    admin = access.create("Recovery")
    server = access.create("Training", "local")
    admin_cookie = access.session(admin, remember=True)
    with browser(db) as web:
        assert login(web, remember=remember).status_code == 200
        old_cookie = web.cookies.get(COOKIE)
        assert web.get("/api/auth/password").json()["requires_current_password"]
        for current in (None, "incorrect"):
            result = web.post(
                "/api/auth/password",
                json={
                    "enabled": True,
                    "password": REPLACEMENT,
                    "current_password": current,
                },
            )
            assert result.status_code == 401
            assert access.authenticate_session(old_cookie)
        changed = web.post(
            "/api/auth/password",
            json={
                "enabled": True,
                "password": REPLACEMENT,
                "current_password": PASSWORD,
            },
        )
        assert changed.status_code == 200
        assert not changed.json()["login_required"]
        assert access.authenticate_session(old_cookie) is None
        assert access.authenticate_password(PASSWORD) is None
        assert access.authenticate_password(REPLACEMENT)
        assert web.get("/api/state").status_code == 200
        assert access.session_remembered(web.cookies.get(COOKIE)) == remember
        assert ("max-age=" in changed.headers["set-cookie"].lower()) == remember
        disabled = web.post(
            "/api/auth/password",
            json={
                "enabled": False,
                "current_password": REPLACEMENT,
            },
        )
        assert disabled.status_code == 200 and disabled.json()["login_required"]
        assert web.cookies.get(COOKIE) is None
        assert web.get("/api/state").status_code == 401
    assert access.authenticate_session(admin_cookie)
    assert access.authenticate(server["token"])


def test_password_management_requires_admin_and_same_origin(db, access):
    admin = access.create("Recovery")
    server = access.create("Training", "local")
    payload = {"enabled": True, "password": REPLACEMENT}
    with browser(db) as web:
        for method in (web.get, web.post):
            assert method("/api/auth/password").status_code == 401
        web.headers["Authorization"] = "Bearer " + server["token"]
        assert web.get("/api/auth/password").status_code == 403
        assert web.post("/api/auth/password", json=payload).status_code == 403
        web.headers["Authorization"] = "Bearer " + admin["token"]
        assert not web.get("/api/auth/password").json()["requires_current_password"]
        assert (
            web.post(
                "/api/auth/password", json=payload, headers={"Origin": "https://foreign.test"}
            ).status_code
            == 403
        )
        del web.headers["X-DeepQueue"]
        assert web.post("/api/auth/password", json=payload).status_code == 403
        web.headers.update(HEADERS)
        # Administrator bearer tokens can reset a forgotten password without knowing it.
        assert web.post("/api/auth/password", json=payload).status_code == 200
        assert access.authenticate_password(REPLACEMENT)
        assert web.post("/api/auth/password", json={"enabled": False}).status_code == 200
        assert web.get("/api/state").status_code == 200


def test_anonymous_local_browser_cannot_enable_password(db):
    with browser(db) as web:
        assert web.get("/api/auth/password").json()["can_configure"] is False
        assert (
            web.post("/api/auth/password", json={"enabled": True, "password": PASSWORD}).status_code
            == 403
        )
    assert not Access(db.home).enabled()


def test_login_validation_never_echoes_secrets(db, access):
    with browser(db) as web:
        for body in (
            {"password": PASSWORD, "token": "fixture-private-token"},
            {"password": {"secret": PASSWORD}},
            {"password": PASSWORD * 20},
            {"password": PASSWORD, "remember": PASSWORD},
            {},
        ):
            response = web.post("/api/auth/login", json=body)
            assert response.status_code == 422
            assert PASSWORD not in response.text and "fixture-private-token" not in response.text
        admin = access.create("Recovery")
        web.headers["Authorization"] = "Bearer " + admin["token"]
        response = web.post("/api/auth/password", json={"enabled": False, "password": PASSWORD})
        assert response.status_code == 422 and PASSWORD not in response.text


def test_password_throttle_recovers_and_does_not_block_token_recovery(db, access, monkeypatch):
    admin = access.create("Recovery")
    with browser(db) as web:
        for _ in range(5):
            assert login(web, "wrong-password").status_code == 401
        throttled = login(web)
        assert throttled.status_code == 429
        assert 1 <= int(throttled.headers["Retry-After"]) <= 61
        assert web.post("/api/auth/login", json={"token": admin["token"]}).status_code == 200
        now = time.monotonic()
        monkeypatch.setattr("deepqueue.access.time.monotonic", lambda: now + 61)
        assert login(web).status_code == 200


def test_concurrent_password_edit_cannot_overwrite_a_newer_change(access):
    previous_id = access.read()["password"]["id"]
    access.set_password(REPLACEMENT, expected_id=previous_id)
    with pytest.raises(ValueError, match="已变化"):
        access.set_password(PASSWORD, expected_id=previous_id)
    with pytest.raises(ValueError, match="已变化"):
        access.disable_password(expected_id=previous_id)
    assert access.authenticate_password(REPLACEMENT)


@pytest.mark.parametrize("source", ["prompt", "env", "file", "stdin"])
def test_local_cli_password_reset_and_disable(db, access, monkeypatch, tmp_path, source):
    arguments = ["--home", str(db.home), "access", "password"]
    options = []
    if source == "env":
        monkeypatch.setenv("FIXTURE_ADMIN_PASSWORD", REPLACEMENT)
        options = ["--password-env", "FIXTURE_ADMIN_PASSWORD"]
    elif source == "file":
        path = tmp_path / "secret"
        path.write_text(REPLACEMENT + "\n")
        options = ["--password-file", str(path)]
    elif source == "stdin":
        monkeypatch.setattr("sys.stdin", io.StringIO(REPLACEMENT + "\r\n"))
        options = ["--password-file", "-"]
    else:
        monkeypatch.setattr("deepqueue.cli.getpass.getpass", lambda prompt: REPLACEMENT)
    result = execute(parser().parse_args([*arguments, "set", *options]))
    assert result["enabled"]
    assert REPLACEMENT not in json.dumps(result)
    assert access.authenticate_password(REPLACEMENT)
    assert execute(parser().parse_args([*arguments, "status"]))["enabled"]
    assert execute(parser().parse_args([*arguments, "disable"]))["enabled"] is False
    assert not access.authenticate_password(REPLACEMENT)
