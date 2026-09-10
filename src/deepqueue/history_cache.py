"""Bounded, connection-local snapshots; Codex remains the source of truth."""

import json
import time
from collections import OrderedDict
from copy import deepcopy


class HistoryCache:
    def __init__(self, entries=64, max_bytes=32 * 1024 * 1024, ttl=20):
        self.entries, self.max_bytes, self.ttl = entries, max_bytes, ttl
        self.rows = OrderedDict()
        self.size = 0

    def get(self, key):
        row = self.rows.get(key)
        if row is None:
            return None
        if time.monotonic() - row[0] >= self.ttl:
            self.discard(key)
            return None
        self.rows.move_to_end(key)
        return deepcopy(row[1])

    def put(self, key, value):
        size = len(json.dumps(value, ensure_ascii=False).encode())
        self.discard(key)
        if size > self.max_bytes:
            return
        self.rows[key] = (time.monotonic(), deepcopy(value), size)
        self.size += size
        while len(self.rows) > self.entries or self.size > self.max_bytes:
            self.discard(next(iter(self.rows)))

    def discard(self, key):
        row = self.rows.pop(key, None)
        if row:
            self.size -= row[2]

    def clear(self):
        self.rows.clear()
        self.size = 0
