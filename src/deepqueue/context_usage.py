"""Connection-local context telemetry, kept independently of the history cache."""

from collections import OrderedDict
from copy import deepcopy


class ContextUsage:
    def __init__(self, limit=256):
        self.limit = limit
        self.rows = OrderedDict()

    def clear(self):
        self.rows.clear()

    def get(self, thread_id):
        return deepcopy(self.rows.get(thread_id, {}))

    def event(self, method, params):
        thread_id = params.get("threadId")
        if not thread_id:
            return None
        if method == "thread/deleted":
            self.rows.pop(thread_id, None)
            return None
        previous = self.rows.get(thread_id, {})
        state = deepcopy(previous)
        if method == "thread/tokenUsage/updated":
            if not isinstance(params.get("tokenUsage"), dict):
                return None
            state["tokenUsage"] = deepcopy(params["tokenUsage"])
        elif (
            method in ("item/started", "item/completed")
            and params.get("item", {}).get("type") == "contextCompaction"
        ):
            state["compaction"] = {
                "turnId": params.get("turnId"),
                "itemId": params["item"]["id"],
                "status": "inProgress" if method == "item/started" else "completed",
            }
        elif method == "thread/compacted":
            # Older app-servers use this completion notification without an item lifecycle.
            state["compaction"] = {
                "turnId": params.get("turnId"),
                "itemId": None,
                "status": "completed",
            }
        elif method == "turn/completed":
            turn = params.get("turn", {})
            compaction = state.get("compaction", {})
            if compaction.get("status") != "inProgress" or compaction.get("turnId") != turn.get(
                "id"
            ):
                return None
            compaction["status"] = {
                "completed": "completed",
                "failed": "failed",
                "interrupted": "interrupted",
            }.get(turn.get("status"), "unknown")
        else:
            return None
        self.rows[thread_id] = state
        self.rows.move_to_end(thread_id)
        while len(self.rows) > self.limit:
            self.rows.popitem(last=False)
        return deepcopy(state) if state != previous else None
