import asyncio

from test_gateway import fixture_gateway, notification

from deepqueue.context_usage import ContextUsage


def event(store, method, **params):
    return store.event(method, {"threadId": "thread-a", "turnId": "turn-a", **params})


def test_compaction_lifecycle_keeps_completed_work_when_later_commands_fail():
    store = ContextUsage()
    item = {"id": "compact-a", "type": "contextCompaction"}
    assert event(store, "item/started", item=item)["compaction"]["status"] == "inProgress"
    assert event(store, "item/completed", item=item)["compaction"]["status"] == "completed"
    event(store, "turn/completed", turn={"id": "turn-a", "status": "failed"})
    assert store.get("thread-a")["compaction"]["status"] == "completed"


def test_compaction_failure_and_interruption_are_not_reported_as_success():
    for terminal in ("failed", "interrupted"):
        store = ContextUsage()
        item = {"id": "compact-a", "type": "contextCompaction"}
        event(store, "item/started", item=item)
        store.event(
            "turn/completed", {"threadId": "thread-a", "turn": {"id": "other", "status": terminal}}
        )
        assert store.get("thread-a")["compaction"]["status"] == "inProgress"
        result = store.event(
            "turn/completed", {"threadId": "thread-a", "turn": {"id": "turn-a", "status": terminal}}
        )
        assert result["compaction"]["status"] == terminal


def test_usage_is_bounded_and_keeps_source_counts_separate():
    store = ContextUsage(limit=2)
    usage = {
        "last": {"totalTokens": 96000},
        "total": {"totalTokens": 540000},
        "modelContextWindow": None,
    }
    result = event(store, "thread/tokenUsage/updated", tokenUsage=usage)
    result["tokenUsage"]["last"]["totalTokens"] = 1
    assert store.get("thread-a")["tokenUsage"] == usage
    for tid in ("thread-b", "thread-c"):
        store.event("thread/tokenUsage/updated", {"threadId": tid, "tokenUsage": usage})
    assert not store.get("thread-a")
    assert len(store.rows) == 2
    assert not ContextUsage().get("thread-c")
    store.clear()
    assert not store.rows


def test_legacy_completion_does_not_invent_an_item():
    store = ContextUsage()
    state = event(store, "thread/compacted")
    assert state["compaction"] == {"turnId": "turn-a", "itemId": None, "status": "completed"}


async def test_usage_survives_history_refresh_and_resets_on_reconnect(db, tmp_path):
    async with fixture_gateway(db, tmp_path) as (gateway, fixture):
        session = await gateway.session("local")
        await session.connect()
        events = asyncio.Queue(maxsize=256)
        session.subscribers.add(events)
        usage = {
            "last": {"totalTokens": 96000},
            "total": {"totalTokens": 540000},
            "modelContextWindow": 128000,
        }
        await fixture.emit(
            "thread/tokenUsage/updated",
            {"threadId": "fixture-task", "turnId": "completed-turn", "tokenUsage": usage},
        )
        state = (await notification(events, "deepqueue/context"))["params"]
        assert state["threadId"] == "fixture-task"
        for force in (False, True):
            result = (await session.read_thread("fixture-task", 40, force=force))["thread"]
            assert result["context"]["tokenUsage"] == usage
            assert result["turns"] == []
        await session.disconnect()
        await session.connect()
        assert not (await session.read_thread("fixture-task", 40))["thread"]["context"]
