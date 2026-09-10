from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import secrets
import time
from contextlib import contextmanager

from .config import private_write

COOKIE = "deepqueue_session"
SESSION_SECONDS = 12 * 3600


class Access:
    def __init__(self, home):
        self.home = home
        self.path = home / "access.json"

    def read(self):
        if not self.path.exists():
            return {"tokens": [], "session_key": ""}
        return json.loads(self.path.read_text())

    @contextmanager
    def edit(self):
        with (self.home / "access.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            yield data
            private_write(self.path, json.dumps(data, indent=2))

    def enabled(self):
        return bool(self.read()["tokens"])

    def has_admin(self):
        return any(item["server"] is None for item in self.read()["tokens"])

    @staticmethod
    def public(item):
        return {key: value for key, value in item.items() if key != "digest"}

    def list(self):
        return [self.public(item) for item in self.read()["tokens"]]

    def create(self, label, server=None):
        token = "dq_" + secrets.token_urlsafe(32)
        item = {
            "id": secrets.token_hex(12),
            "label": label,
            "server": server,
            "created_at": time.time(),
            "digest": hashlib.sha256(token.encode()).hexdigest(),
        }
        with self.edit() as data:
            data["session_key"] = data["session_key"] or secrets.token_hex(32)
            data["tokens"].append(item)
        return {**self.public(item), "token": token}

    def revoke(self, token_id):
        with self.edit() as data:
            item = next((item for item in data["tokens"] if item["id"] == token_id), None)
            if item is None:
                raise ValueError("Unknown access token")
            if (
                item["server"] is None
                and sum(item["server"] is None for item in data["tokens"]) == 1
            ):
                raise ValueError(
                    "Create a replacement administrator token before revoking this one"
                )
            data["tokens"] = [item for item in data["tokens"] if item["id"] != token_id]

    def authenticate(self, token):
        if not token or len(token) > 1024 or not token.isascii():
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for item in self.read()["tokens"]:
            if hmac.compare_digest(item["digest"], digest):
                return self.public(item)
        return None

    def session(self, principal):
        expires = int(time.time()) + SESSION_SECONDS
        message = f"{principal['id']}.{expires}.{secrets.token_hex(12)}"
        signature = hmac.new(
            self.read()["session_key"].encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return message + "." + signature

    def authenticate_session(self, cookie):
        if not cookie or len(cookie) > 512 or not cookie.isascii():
            return None
        parts = cookie.split(".")
        if len(parts) != 4:
            return None
        token_id, expiry, _, signature = parts
        if not expiry.isdigit() or int(expiry) < time.time():
            return None
        data = self.read()
        expected = hmac.new(
            data["session_key"].encode(), ".".join(parts[:3]).encode(), hashlib.sha256
        ).hexdigest()
        if not data["session_key"] or not hmac.compare_digest(signature, expected):
            return None
        return next(
            (
                self.public(item)
                for item in data["tokens"]
                if item["id"] == token_id and item["server"] is None
            ),
            None,
        )
