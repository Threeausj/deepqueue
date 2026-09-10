import time

from fastapi.testclient import TestClient

from deepqueue.config import Secrets
from deepqueue.models import JobSpec, Resources
from deepqueue.web import create_app


def client(db):
    return TestClient(
        create_app(db.home), base_url="http://localhost", headers={"X-DeepQueue": "1"}
    )


def test_browser_submit_control_and_shared_queue_state(db):
    web = client(db)
    manifest = {
        "name": "browser-batch",
        "defaults": {"server": "local", "cwd": str(db.home)},
        "matrix": {"seed": [1, 2]},
        "experiments": [
            {"key": "trial-{index}", "argv": ["python", "train.py", "--seed", "{seed}"]}
        ],
    }
    preview = web.post("/api/batches/preview", json=manifest)
    assert preview.status_code == 200 and preview.json()["experiment_count"] == 2
    assert not db.jobs()
    submitted = web.post("/api/batches", json=manifest)
    assert submitted.status_code == 200
    first, second = submitted.json()["jobs"].values()
    assert (
        web.post(
            f"/api/jobs/{second}/control", json={"action": "priority", "priority": 40}
        ).status_code
        == 200
    )
    state = web.get("/api/state").json()
    assert next(job for job in state["jobs"] if job["id"] == second)["position"] == 1
    assert db.job(second)["priority"] == 40
    assert (
        web.post("/api/batch/control?name=browser-batch", json={"action": "pause"}).status_code
        == 200
    )
    assert not db.agent_candidates()
    assert web.post(f"/api/jobs/{first}/control", json={"action": "pause"}).status_code == 200
    assert (
        web.post("/api/batch/control?name=browser-batch", json={"action": "resume"}).status_code
        == 200
    )
    assert [job["id"] for job in db.agent_candidates()] == [second]
    report = web.get("/api/batch/report?name=browser-batch").json()
    assert report["experiments"][0]["pause_sources"] == ["job"]


def test_api_rejects_cross_origin_writes_and_untrusted_hosts(db):
    web = client(db)
    assert web.get("/api/state", headers={"Host": "attacker.example"}).status_code == 400
    assert (
        web.post("/api/jobs", headers={"Origin": "https://other.example"}, json={}).status_code
        == 403
    )
    assert web.post("/api/jobs", headers={"X-DeepQueue": ""}, json={}).status_code == 403
    assert web.post("/api/jobs", headers={"Origin": "null"}, json={}).status_code == 403
    assert web.post("/api/jobs", headers={"Origin": "http://localhost"}, json={}).status_code == 422
    assert not db.jobs()


def test_api_preserves_links_and_results_without_exposing_environment(db):
    job = db.submit(
        JobSpec(
            server="local",
            cwd=str(db.home),
            command="true",
            resources=Resources(),
            skip_estimate=True,
            env={"API_TOKEN": "private-token"},
        )
    )
    db.reserve(job["id"], [])
    db.finish(job["id"], {"status": "succeeded", "exit_code": 0, "metrics": {"peak_ram_mib": 7}})
    agent = db.start_agent(job["id"], "archive", "test-fixture")
    db.agent_link(agent, "real-record-id", "codex://threads/real-record-id")
    db.complete_agent(
        agent,
        {
            "summary": "fixture summary",
            "outcome": "success",
            "findings": [],
            "resource_assessment": "7 MiB",
            "next_steps": ["Inspect metrics"],
            "suggested_command": None,
        },
    )
    web = client(db)
    response = web.get(f"/api/jobs/{job['id']}")
    assert response.status_code == 200
    assert "private-token" not in response.text
    detail = response.json()
    assert detail["spec"]["environment_variable_names"] == ["API_TOKEN"]
    assert detail["analysis"]["summary"] == "fixture summary"
    assert detail["agents"][0]["deep_link"] == "codex://threads/real-record-id"
    assert detail["runs"][0]["result"]["metrics"]["peak_ram_mib"] == 7
    assert web.get(f"/api/jobs/{job['id']}/logs?size=999999999").status_code == 422


def test_invalid_web_controls_do_not_change_jobs(db):
    job = db.submit(JobSpec(server="local", cwd=str(db.home), command="true"))
    web = client(db)
    path = f"/api/jobs/{job['id']}/control"
    assert web.post(path, json={"action": "priority", "priority": 500}).status_code == 422
    assert web.post(path, json={"action": "priority"}).status_code == 400
    assert web.post(path, json={"action": "approve"}).status_code == 400
    assert db.job(job["id"])["status"] == "pending"
    assert db.job(job["id"])["priority"] == 0


def test_browser_completion_controls_and_empty_terminal(db):
    web = client(db)
    submitted = web.post(
        "/api/jobs",
        json={
            "server": "local",
            "cwd": str(db.home),
            "command": "true",
            "source_thread_id": "browser-source",
            "completion_mode": "improve",
            "max_improvement_rounds": 2,
            "resources": {"gpu_count": 1},
            "skip_estimate": True,
        },
    )
    assert submitted.status_code == 200
    job_id = submitted.json()["id"]
    assert submitted.json()["spec"]["tmux"] and submitted.json()["spec"]["launch_agent"]
    assert web.get(f"/api/jobs/{job_id}/terminal").json() == {"available": False}
    response = web.post(
        f"/api/jobs/{job_id}/control",
        json={
            "action": "mode",
            "completion_mode": "finish",
        },
    )
    assert response.status_code == 200 and response.json()["completion_mode"] == "finish"
    assert web.get("/api/state").json()["jobs"][0]["completion_mode"] == "finish"
    assert (
        web.post(
            f"/api/jobs/{job_id}/control",
            json={
                "action": "mode",
                "completion_mode": "improve",
            },
        ).status_code
        == 200
    )


def test_web_exposes_and_releases_improvement_reservations(db):
    web = client(db)
    job = db.submit(
        JobSpec(
            server="local",
            cwd=str(db.home),
            command="true",
            source_thread_id="source",
            completion_mode="improve",
            resources=Resources(gpu_count=1),
            skip_estimate=True,
        )
    )
    db.reserve(job["id"], [{"index": 0, "uuid": "GPU-fixture", "name": "fixture"}])
    db.finish(job["id"], {"status": "succeeded"})
    assert web.get("/api/state").json()["servers"][0]["reservations"][0]["holding"]
    web.post(
        f"/api/jobs/{job['id']}/control",
        json={
            "action": "mode",
            "completion_mode": "finish",
        },
    )
    assert web.get("/api/state").json()["servers"][0]["reservations"] == []


def test_web_distinguishes_fresh_and_expired_hardware_snapshots(db):
    web = client(db)
    assert web.get("/api/state").json()["servers"][0]["snapshot_stale"]
    db.snapshot("local", {"received_at": time.time(), "gpus": []})
    assert not web.get("/api/state").json()["servers"][0]["snapshot_stale"]
    db.snapshot("local", {"received_at": time.time() - 60, "gpus": []})
    assert web.get("/api/state").json()["servers"][0]["snapshot_stale"]


def test_web_ssh_form_keeps_password_in_encrypted_vault(db, monkeypatch):
    trusted = []
    monkeypatch.setattr(
        "deepqueue.web.trust_host",
        lambda home, host, port, fingerprint: trusted.append((host, port, fingerprint)),
    )
    web = client(db)
    response = web.post(
        "/api/servers",
        json={
            "name": "ssh-fixture",
            "host": "192.0.2.1",
            "username": "researcher",
            "fingerprint": "SHA256:test-fixture",
            "password": "fixture-password",
        },
    )
    assert response.status_code == 200
    assert trusted == [("192.0.2.1", 22, "SHA256:test-fixture")]
    assert "fixture-password" not in response.text and "password_ref" not in response.text
    reference = db.server("ssh-fixture")["config"]["password_ref"]
    assert Secrets(db.home).get(reference) == "fixture-password"
    assert all(
        b"fixture-password" not in path.read_bytes() for path in (db.home / "secrets").iterdir()
    )


def test_web_batch_names_accept_slashes_and_query_characters(db):
    name = "2026/09/06 round?seed=7&lr=0.01"
    db.submit_batch(
        {
            "name": name,
            "defaults": {"server": "local", "cwd": str(db.home)},
            "experiments": [{"key": "trial", "command": "true"}],
        }
    )
    web = client(db)
    assert web.get("/api/batch", params={"name": name}).json()["name"] == name
    assert (
        web.post("/api/batch/control", params={"name": name}, json={"action": "pause"}).status_code
        == 200
    )
    assert db.batch(name)["paused"]
    assert web.get("/api/batch/report", params={"name": name}).json()["counts"] == {"pending": 1}
