import asyncio
import copy

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_gateway import fixture_gateway, notification

from deepqueue.access import Access
from deepqueue.cli import execute, parser
from deepqueue.gateway import AccessOptions, ForkThread, Message, NewThread
from deepqueue.web import create_app


async def test_permissions_are_native_and_survive_reconnect_without_new_turns(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        catalog = await session.permissions("/tmp/project")
        assert not catalog["data"][-1]["allowed"]
        assert (
            next(c for c in fixture.calls if c.get("method") == "permissionProfile/list")["params"][
                "cwd"
            ]
            == "/tmp/project"
        )
        tid = (
            await session.new_thread(
                NewThread(
                    cwd="/tmp/project",
                    permissions=":read-only",
                    approval_policy="never",
                )
            )
        )["thread"]["id"]
        result = await session.update_access(
            tid, AccessOptions(permissions=":workspace", approval_policy="on-request")
        )
        assert result["thread"]["sandbox"]["type"] == "workspaceWrite"
        assert result["thread"]["approvalPolicy"] == "on-request"
        # Native Codex does not broadcast another event when saving the same values.
        async with asyncio.timeout(1):
            unchanged = await session.update_access(
                tid, AccessOptions(permissions=":workspace", approval_policy="on-request")
            )
        assert unchanged["thread"]["activePermissionProfile"]["id"] == ":workspace"
        assert not any(c.get("method") == "turn/start" for c in fixture.calls)
        # Persisted tasks can be resumed after the gateway forgets all cached settings.
        await session.disconnect()
        await session.connect()
        result = (await session.open_thread(tid, 40))["thread"]
        assert result["activePermissionProfile"]["id"] == ":workspace"
        previous = copy.deepcopy(result)
        with pytest.raises(HTTPException, match="not allowed"):
            await session.update_access(tid, AccessOptions(permissions="restricted-by-admin"))
        assert (await session.open_thread(tid, 40))["thread"] == previous
        fixture.requirements = {"allowedApprovalPolicies": ["on-request"]}
        with pytest.raises(HTTPException, match="not allowed"):
            await session.update_access(tid, AccessOptions(approval_policy="never"))
        assert fixture.threads[tid]["approvalPolicy"] == "on-request"


async def test_external_permission_notifications_and_send_overrides(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        queue = asyncio.Queue(maxsize=256)
        session.subscribers.add(queue)
        await session.open_thread("fixture-task", 40)
        await session.rpc(
            "thread/settings/update",
            {
                "threadId": "fixture-task",
                "permissions": ":read-only",
                "approvalPolicy": "never",
            },
        )
        await notification(queue, "thread/settings/updated")
        assert (await session.read_thread("fixture-task", 40))["thread"]["sandbox"][
            "type"
        ] == "readOnly"
        await session.send_message(
            "fixture-task",
            Message(
                text="permissions",
                request_id="p",
                permissions=":workspace",
                approval_policy="on-request",
            ),
        )
        await notification(queue, "turn/completed")
        assert fixture.threads["fixture-task"]["sandbox"]["type"] == "workspaceWrite"
        sent = next(c for c in fixture.calls if c.get("method") == "turn/start")
        assert sent["params"]["permissions"] == ":workspace"


async def test_native_turn_fork_preserves_source_project_and_permissions_and_deduplicates(
    db, tmp_path
):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        fixture.add_project("project", "Project", ["/tmp/codex-fixture"])
        source = fixture.threads["fixture-task"]
        source["projectId"] = "project"
        source["turns"] = [
            {
                "id": f"t-{i}",
                "status": "completed",
                "items": [{"id": f"m-{i}", "type": "agentMessage", "text": str(i)}],
            }
            for i in range(3)
        ]
        before = copy.deepcopy(source)
        session = await gateway.session("local")
        await session.connect()
        data = ForkThread(last_turn_id="t-0", request_id="fork-once")
        first, repeated = await asyncio.gather(
            session.fork_thread(source["id"], data), session.fork_thread(source["id"], data)
        )
        assert first == repeated
        fork = first["thread"]
        assert fork["id"] != source["id"]
        assert fork["forkedFromId"] == source["id"]
        assert fork["projectId"] == "project"
        assert fork["turns"] == before["turns"][:1]
        assert fork["activePermissionProfile"] == source["activePermissionProfile"]
        assert fork["reasoningEffort"] == source["reasoningEffort"]
        assert source == before
        calls = [c for c in fixture.calls if c.get("method") == "thread/fork"]
        assert len(calls) == 1 and calls[0]["params"]["deferGoalContinuation"]
        assert not any(c.get("method") in ("thread/rollback", "turn/start") for c in fixture.calls)
        with pytest.raises(HTTPException, match="request_id"):
            await session.fork_thread(source["id"], data.model_copy(update={"last_turn_id": "t-1"}))
        await session.disconnect()
        await session.connect()
        assert await session.fork_thread(source["id"], data) == first
        queue = asyncio.Queue(maxsize=256)
        session.subscribers.add(queue)
        await session.send_message(fork["id"], Message(text="Only branch changes", request_id="b"))
        await notification(queue, "turn/completed")
        assert source == before
        assert len(fixture.threads[fork["id"]]["turns"]) == 2


async def test_fork_rejects_invalid_boundary_but_can_branch_before_active_turn(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        source = fixture.threads["fixture-task"]
        with pytest.raises(HTTPException, match="一轮"):
            await session.fork_thread(source["id"], ForkThread(request_id="empty"))
        source["turns"] = [
            {"id": "finished", "status": "completed", "items": []},
            {"id": "working", "status": "inProgress", "items": []},
        ]
        for boundary in (None, "working", "other-task-turn"):
            with pytest.raises(HTTPException):
                await session.fork_thread(
                    source["id"], ForkThread(last_turn_id=boundary, request_id="invalid")
                )
        fork = await session.fork_thread(
            source["id"], ForkThread(last_turn_id="finished", request_id="valid")
        )
        assert len(fork["thread"]["turns"]) == 1
        assert source["turns"][-1]["status"] == "inProgress"


async def test_fork_preserves_exact_custom_sandbox_and_uncertain_result_is_not_replayed(
    db, tmp_path, monkeypatch
):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        source = fixture.threads["fixture-task"]
        source.update(
            activePermissionProfile=None,
            sandbox={
                "type": "workspaceWrite",
                "writableRoots": ["/special"],
                "networkAccess": True,
            },
        )
        source["turns"] = [{"id": "t", "status": "completed", "items": []}]
        fork = await session.fork_thread(source["id"], ForkThread(request_id="custom"))
        assert fixture.threads[fork["thread"]["id"]]["sandbox"] == source["sandbox"]
        actual_request = session.client.request
        calls = []

        async def lost_response(method, params):
            if method == "thread/fork":
                calls.append(params)
                raise TimeoutError("lost")
            return await actual_request(method, params)

        monkeypatch.setattr(session.client, "request", lost_response)
        for _ in range(2):
            with pytest.raises(HTTPException, match="待确认"):
                await session.fork_thread(source["id"], ForkThread(request_id="lost"))
        assert len(calls) == 1


async def test_http_actions_validate_and_never_fall_through_to_other_server(db, tmp_path):
    async with fixture_gateway(db, tmp_path):
        app = create_app(db.home)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
                headers={"X-DeepQueue": "1"},
            ) as client:
                base = "/api/servers/local/codex"
                await client.post(base + "/connect", json={})
                assert (await client.get(base + "/permissions")).is_success
                for suffix, data in [
                    ("access", {"permissions": ":workspace"}),
                    ("fork", {"request_id": "f"}),
                    ("name", {"name": "Renamed"}),
                ]:
                    assert (
                        await client.post(f"{base}/threads/other-server-task/{suffix}", json=data)
                    ).status_code == 502
                for data in [
                    {},
                    {"approval_policy": "always"},
                    {"permissions": ":workspace", "sandbox": "anything"},
                ]:
                    assert (
                        await client.post(f"{base}/threads/fixture-task/access", json=data)
                    ).status_code == 422
                assert (
                    await client.post(f"{base}/threads/fixture-task/name", json={"name": "  "})
                ).status_code == 422
                assert (
                    await client.post(
                        f"{base}/threads/fixture-task/name", json={"name": "  Renamed  "}
                    )
                ).json()["name"] == "Renamed"
                assert (await client.get(f"{base}/threads/fixture-task")).json()["thread"][
                    "name"
                ] == "Renamed"
        finally:
            await app.state.codex.close()


def test_codex_actions_remain_admin_only_and_require_same_origin(db):
    access = Access(db.home)
    admin = access.create("Admin")
    scoped = access.create("Submitter", server="local")
    base = "/api/servers/local/codex"
    actions = {
        "/threads/t/access": {"permissions": ":danger-full-access"},
        "/threads/t/fork": {"request_id": "fork"},
        "/threads/t/name": {"name": "New name"},
    }
    with TestClient(create_app(db.home), base_url="http://localhost") as client:
        assert client.get(base + "/permissions").status_code == 401
        for path, data in actions.items():
            assert (
                client.post(base + path, json=data, headers={"X-DeepQueue": "1"}).status_code == 401
            )
            assert (
                client.post(
                    base + path,
                    json=data,
                    headers={"X-DeepQueue": "1", "Authorization": "Bearer " + scoped["token"]},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    base + path,
                    json=data,
                    headers={
                        "X-DeepQueue": "1",
                        "Authorization": "Bearer " + admin["token"],
                        "Origin": "https://elsewhere.test",
                    },
                ).status_code
                == 403
            )


@pytest.mark.parametrize(
    "argv,path,data,query",
    [
        (["permissions", "local", "--cwd", "/project"], "/permissions", None, {"cwd": "/project"}),
        (
            [
                "access",
                "local",
                "task",
                "--permissions",
                ":read-only",
                "--approval-policy",
                "never",
            ],
            "/threads/task/access",
            {"permissions": ":read-only", "approval_policy": "never"},
            {},
        ),
        (
            ["fork", "local", "task", "--last-turn-id", "turn", "--request-id", "once"],
            "/threads/task/fork",
            {"last_turn_id": "turn", "request_id": "once"},
            {},
        ),
        (
            ["rename", "local", "task", "--name", "训练分析"],
            "/threads/task/name",
            {"name": "训练分析"},
            {},
        ),
        (
            ["new", "local", "--cwd", "/project", "--permissions", ":workspace"],
            "/threads",
            {"cwd": "/project", "permissions": ":workspace"},
            {},
        ),
    ],
)
def test_cli_task_actions_contract(tmp_path, monkeypatch, argv, path, data, query):
    calls = []
    monkeypatch.setenv("DEEPQUEUE_CLIENT_CONFIG", str(tmp_path / "empty.json"))
    monkeypatch.setattr(
        "deepqueue.remote.Client.request",
        lambda self, path, data=None, **kw: calls.append((path, data, kw)) or {},
    )
    execute(parser().parse_args(["--url", "https://queue.example", "codex", *argv]))
    assert calls == [("/servers/local/codex" + path, data, query)]
