import sqlite3

import pytest
from fastapi.testclient import TestClient

from deepqueue.access import Access
from deepqueue.cli import execute, parser
from deepqueue.db import SCHEMA_VERSION, Database
from deepqueue.models import JobSpec, Server
from deepqueue.web import create_app


@pytest.fixture
def workspace_batch(db):
    db.add_server(Server(name="remote", worker_root=str(db.home / "remote")))
    manifest = {
        "name": "shared/batch & trial",
        "defaults": {"server": "local", "cwd": str(db.home), "command": "true"},
        "experiments": [
            {"key": "local-one"},
            {"key": "local-two"},
            {"key": "remote-one", "server": "remote"},
        ],
    }
    ids = db.submit_batch(manifest)["jobs"]
    return manifest, ids


def test_server_batch_pause_gates_admission_without_releasing_other_holds(db, workspace_batch):
    manifest, ids = workspace_batch
    name = manifest["name"]
    db.set_paused(ids["local-one"], True)
    db.set_batch_paused(name, True, "local")
    assert db.job(ids["local-one"])["pause_sources"] == ["job", "batch"]
    assert {job["id"] for job in db.agent_candidates()} == {ids["remote-one"]}
    assert db.start_agent(ids["local-two"], "estimate", "fixture") is None
    assert {job["id"] for job in db.jobs(dispatchable=True)} == {ids["remote-one"]}
    restarted = Database(db.home)
    assert restarted.batch(name, "local")["paused"]
    assert not restarted.batch(name, "remote")["paused"]
    restarted.set_batch_paused(name, False, "local")
    assert restarted.job(ids["local-one"])["pause_sources"] == ["job"]
    assert not restarted.job(ids["local-two"])["pause_sources"]
    # A local resume can override a pre-existing global pause while other servers remain held.
    restarted.set_batch_paused(name, True)
    restarted.set_batch_paused(name, False, "local")
    assert restarted.job(ids["remote-one"])["pause_sources"] == ["batch"]
    restarted.set_batch_paused(name, False)
    assert not restarted.job(ids["remote-one"])["pause_sources"]
    assert restarted.job(ids["local-one"])["pause_sources"] == ["job"]


def test_scoped_api_and_cli_control_only_the_selected_servers_members(db, workspace_batch):
    manifest, ids = workspace_batch
    name = manifest["name"]
    db.submit_batch(
        {**manifest, "name": "remote-only", "experiments": [manifest["experiments"][-1]]}
    )
    web = TestClient(create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"})
    query = {"name": name, "server": "local"}
    state = web.get("/api/state", params={"server": "local"}).json()
    assert state["scope_server"] == "local"
    assert {row["server"] for row in state["jobs"]} == {"local"}
    assert [row["name"] for row in state["batches"]] == [name]
    assert {row["name"] for row in state["servers"]} == {"local", "remote"}
    response = web.post(
        "/api/batch/control", params=query, json={"action": "priority", "priority": 70}
    )
    assert response.json() == {"updated": 2, "eligible": 2, "total": 2}
    assert db.job(ids["remote-one"])["priority"] == 0
    assert web.post("/api/batch/control", params=query, json={"action": "pause"}).status_code == 200
    assert web.get("/api/batches", params={"server": "local"}).json()[0]["paused"]
    report = web.get("/api/batch/report", params=query).json()
    assert {row["id"] for row in report["experiments"]} == {ids["local-one"], ids["local-two"]}
    assert report["counts"] == {"pending": 2}
    execute(
        parser().parse_args(["--home", str(db.home), "batch", "cancel", name, "--server", "local"])
    )
    assert db.job(ids["local-two"])["status"] == "cancelled"
    assert db.job(ids["remote-one"])["status"] == "pending"
    assert (
        web.get("/api/batch", params={"name": "remote-only", "server": "local"}).status_code == 400
    )
    assert not web.get("/api/state", params={"server": "missing"}).json()["jobs"]


def test_workspace_submission_rejects_foreign_servers_before_inserting(db, workspace_batch):
    manifest, _ = workspace_batch
    web = TestClient(create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"})
    before = len(db.jobs())
    for endpoint in ("/api/batches/preview", "/api/batches"):
        response = web.post(
            endpoint, params={"server": "local"}, json={**manifest, "name": "wrong-scope"}
        )
        assert response.status_code == 422
    spec = JobSpec(server="remote", cwd=str(db.home), command="true")
    assert (
        web.post("/api/jobs", params={"server": "local"}, json=spec.model_dump()).status_code == 422
    )
    assert len(db.jobs()) == before


def test_v6_migration_preserves_global_pauses_and_membership(db, workspace_batch):
    manifest, ids = workspace_batch
    db.set_batch_paused(manifest["name"], True)
    with db.connection(write=True) as con:
        con.execute("DROP TABLE batch_server_pauses")
        con.execute("PRAGMA user_version=6")
    db.ensure_schema()
    assert db.schema_version() == SCHEMA_VERSION
    assert db.submit_batch(manifest)["existing"]
    assert all(db.job(job_id)["pause_sources"] == ["batch"] for job_id in ids.values())
    assert db.batch(manifest["name"], "remote")["paused"]
    with sqlite3.connect(db.path) as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bound_credentials_cannot_change_the_server_query_to_escape_their_scope(
    db, workspace_batch
):
    manifest, ids = workspace_batch
    access = Access(db.home)
    access.create("Administrator")
    token = access.create("Local submitter", "local")["token"]
    web = TestClient(
        create_app(db.home),
        base_url="http://localhost",
        headers={"X-DeepQueue": "1", "Authorization": f"Bearer {token}"},
    )
    query = {"name": manifest["name"], "server": "remote"}
    for endpoint in ("/api/batches", "/api/batch", "/api/batch/report"):
        assert web.get(endpoint, params=query).status_code == 403
    assert (
        web.post("/api/batch/control", params=query, json={"action": "cancel"}).status_code == 403
    )
    query["server"] = "local"
    report = web.get("/api/batch/report", params=query)
    assert report.status_code == 200
    assert {row["id"] for row in report.json()["experiments"]} == {
        ids["local-one"],
        ids["local-two"],
    }
    assert web.post("/api/batch/control", params=query, json={"action": "pause"}).status_code == 200
    assert not db.job(ids["remote-one"])["pause_sources"]
