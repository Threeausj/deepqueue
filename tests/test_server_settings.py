import json
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_public import live_http

from deepqueue.access import Access
from deepqueue.cli import execute, parser
from deepqueue.config import Secrets
from deepqueue.db import Database
from deepqueue.models import JobSpec, Resources, Server
from deepqueue.servers import ServerUpdate, update_server
from deepqueue.web import create_app


def ssh_server(db):
    secret = Secrets(db.home).put("original-password")
    db.add_server(
        Server(
            name="gpu-a", kind="ssh", host="original.test", username="runner", password_ref=secret
        )
    )
    db.snapshot("gpu-a", {"received_at": time.time(), "gpus": []})
    return db.server("gpu-a")


def test_edit_metadata_preserves_experiment_identity_and_credentials(db):
    original = ssh_server(db)
    spec = JobSpec(server="gpu-a", cwd="/project", command="true", idempotency_key="stable")
    job = db.submit(spec)
    token = Access(db.home).create("GPU submission", "gpu-a")
    with db.connection(write=True) as con:
        con.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    saved = update_server(
        db.home, "gpu-a", ServerUpdate(display_name="  训练服务器 A  ", max_running=3)
    )
    assert saved["name"] == "gpu-a"
    assert saved["config"]["display_name"] == "训练服务器 A"
    assert saved["config"]["max_running"] == 3
    assert saved["config"]["password_ref"] == original["config"]["password_ref"]
    assert saved["snapshot"] == original["snapshot"]
    assert db.submit(spec)["id"] == job["id"]
    assert db.job(job["id"])["spec"] == job["spec"]
    assert Access(db.home).authenticate(token["token"])["server"] == "gpu-a"
    assert Database(db.home).servers()[-1]["config"]["display_name"] == "训练服务器 A"


def test_server_order_survives_restart_and_rejects_stale_or_duplicate_lists(db):
    db.add_server(Server(name="alpha"))
    # Pre-upgrade records have neither field and retain their original alphabetic order.
    with db.connection(write=True) as con:
        con.execute(
            "UPDATE servers SET config=json_remove(config, '$.sort_order', '$.display_name')"
        )
    assert [row["name"] for row in db.servers()] == ["alpha", "local"]
    db.reorder_servers(["local", "alpha"])
    assert [row["name"] for row in Database(db.home).servers()] == ["local", "alpha"]
    db.add_server(Server(name="aaa-new"))
    expected = db.servers()
    assert [row["name"] for row in expected] == ["local", "alpha", "aaa-new"]
    for names in (["local", "alpha"], ["local", "alpha", "alpha"], ["other", "alpha", "aaa-new"]):
        with pytest.raises(ValueError):
            db.reorder_servers(names)
        assert db.servers() == expected


@pytest.mark.parametrize(
    "activity", ["estimating", "starting", "running", "lost", "archive", "hold"]
)
def test_connection_edits_do_not_redirect_active_work(db, monkeypatch, activity):
    original = ssh_server(db)
    job = db.submit(
        JobSpec(
            server="gpu-a",
            cwd="/project",
            command="true",
            resources=Resources(ram_mib=128),
            skip_estimate=True,
        )
    )
    run = db.reserve(job["id"], [])
    with db.connection(write=True) as con:
        con.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            ("succeeded" if activity in {"archive", "hold"} else activity, job["id"]),
        )
        if activity == "hold":
            con.execute(
                "INSERT INTO resource_holds VALUES(?,?,?)", (run, job["id"], time.time() + 60)
            )
    if activity == "archive":
        assert db.start_agent(job["id"], "archive", "fixture")
    trust = Mock()
    monkeypatch.setattr("deepqueue.servers.trust_host", trust)
    with pytest.raises(ValueError, match="仍有实验或 Agent"):
        update_server(
            db.home,
            "gpu-a",
            ServerUpdate(
                host="other.test",
                fingerprint="verified",
                authentication="password",
                password="new-password",
            ),
        )
    assert db.server("gpu-a") == original
    trust.assert_not_called()
    assert len(list((db.home / "secrets").iterdir())) == 1
    # Display changes remain usable while training runs.
    update_server(db.home, "gpu-a", ServerUpdate(display_name="Renamed while busy"))


def test_connection_edit_requires_fingerprint_and_keeps_secrets_out_of_api(db, monkeypatch):
    original = ssh_server(db)
    trust = Mock()
    monkeypatch.setattr("deepqueue.servers.trust_host", trust)
    with TestClient(
        create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"}
    ) as web:
        url = "/api/servers/gpu-a/settings"
        assert web.post(url, json={"host": "next.test"}).status_code == 400
        assert db.server("gpu-a") == original
        changed = web.post(
            url,
            json={
                "host": "next.test",
                "port": 2222,
                "fingerprint": "verified",
                "authentication": "password",
                "password": "replacement",
            },
        )
        assert changed.status_code == 200, changed.text
        trust.assert_called_once_with(db.home, "next.test", 2222, "verified")
        assert changed.json()["snapshot"] is None
        for key in ("key_file", "password_ref", "passphrase_ref", "password"):
            assert key not in changed.json()["config"]
        assert "replacement" not in changed.text
        saved = db.server("gpu-a")["config"]
        assert Secrets(db.home).get(saved["password_ref"]) == "replacement"
        assert "replacement" not in db.path.read_bytes().decode(errors="ignore")
        # An unrelated edit retains the saved login without asking for the password again.
        assert web.post(url, json={"display_name": "新名称"}).status_code == 200
        assert db.server("gpu-a")["config"]["password_ref"] == saved["password_ref"]
        key = web.post(
            url,
            json={
                "authentication": "key",
                "key_file": "/keys/runner",
                "passphrase": "key-passphrase",
            },
        )
        assert key.status_code == 200, key.text
        saved = db.server("gpu-a")["config"]
        assert saved["password_ref"] is None
        assert saved["key_file"] == "/keys/runner"
        assert Secrets(db.home).get(saved["passphrase_ref"]) == "key-passphrase"
        for invalid in (
            {"name": "new-id"},
            {"password": "ignored"},
            {"display_name": "  "},
            {"max_running": 0},
            {"authentication": "password"},
        ):
            assert web.post(url, json=invalid).status_code in (400, 422)
        assert db.server("gpu-a")["config"] == saved


def test_new_server_management_routes_require_admin_and_origin(db):
    access = Access(db.home)
    admin = access.create("Admin")
    scoped = access.create("Submitter", "local")
    paths = [
        ("/api/servers/order", {"names": ["local"]}),
        ("/api/servers/local/settings", {"display_name": "edited"}),
    ]
    with TestClient(create_app(db.home), base_url="http://localhost") as web:
        for path, body in paths:
            assert web.post(path, json=body, headers={"X-DeepQueue": "1"}).status_code == 401
            for credential, status in ((scoped, 403), (admin, 200)):
                headers = {"Authorization": "Bearer " + credential["token"], "X-DeepQueue": "1"}
                assert web.post(path, json=body, headers=headers).status_code == status
                assert (
                    web.post(
                        path, json=body, headers={**headers, "Origin": "https://foreign.test"}
                    ).status_code
                    == 403
                )


def test_local_and_remote_cli_can_edit_and_reorder(db, tmp_path, monkeypatch):
    ssh_server(db)
    parse = parser().parse_args
    edited = execute(
        parse(["--home", str(db.home), "server", "update", "gpu-a", "--display-name", "训练 A"])
    )
    assert edited["config"]["display_name"] == "训练 A"
    access = Access(db.home)
    admin = access.create("Admin")
    monkeypatch.setenv("DEEPQUEUE_CLIENT_CONFIG", str(tmp_path / "unused-client.json"))
    monkeypatch.setenv("DEEPQUEUE_TOKEN", admin["token"])
    monkeypatch.delenv("DEEPQUEUE_SERVER", raising=False)
    monkeypatch.delenv("DEEPQUEUE_TOKEN_ENV", raising=False)
    settings = tmp_path / "server-update.json"
    settings.write_text(json.dumps({"display_name": "远程编辑", "max_running": 4}))
    with live_http(create_app(db.home)) as url:
        saved = execute(parse(["--url", url, "server", "update", "gpu-a", "--file", str(settings)]))
        assert saved["config"]["display_name"] == "远程编辑"
        assert saved["config"]["max_running"] == 4
        ordered = execute(parse(["--url", url, "server", "order", "gpu-a", "local"]))
        assert [row["name"] for row in ordered] == ["gpu-a", "local"]
        assert [row["name"] for row in db.servers()] == ["gpu-a", "local"]


def test_stale_connection_cannot_publish_resources_or_reserve_a_job(db):
    old = Server.model_validate(ssh_server(db)["config"])
    job = db.submit(
        JobSpec(
            server="gpu-a",
            cwd="/project",
            command="true",
            resources=Resources(ram_mib=128),
            skip_estimate=True,
        )
    )
    update_server(db.home, "gpu-a", ServerUpdate(username="new-runner"))
    assert not db.snapshot("gpu-a", {"gpus": []}, expected_server=old)
    assert db.server("gpu-a")["snapshot"] is None
    assert db.reserve(job["id"], [], expected_server=old) is None
    assert db.job(job["id"])["status"] == "queued"
    latest = Server.model_validate(db.server("gpu-a")["config"])
    update_server(db.home, "gpu-a", ServerUpdate(display_name="Display-only change"))
    db.reorder_servers(["gpu-a", "local"])
    assert db.snapshot("gpu-a", {"gpus": []}, expected_server=latest)
    assert db.reserve(job["id"], [], expected_server=latest)
