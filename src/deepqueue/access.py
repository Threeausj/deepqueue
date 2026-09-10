from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager

from .config import private_write

COOKIE = "deepqueue_session"
SESSION_SECONDS = 12 * 3600
REMEMBER_SECONDS = 30 * 24 * 3600
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


def validate_password(password):
    if (
        not isinstance(password, str)
        or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH
    ):
        raise ValueError("管理员密码需要 12–128 个字符")
    if not password.strip():
        raise ValueError("管理员密码不能全部为空白字符")
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("管理员密码包含无效字符") from None
    return password


def password_digest(password, salt):
    # OWASP's 32 MiB scrypt profile: N=2^15, r=8, p=3.
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=2**15,
        r=8,
        p=3,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    ).hex()


class LoginThrottled(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after) + 1)
        super().__init__("密码尝试过于频繁，请稍后重试")


class PasswordThrottle:
    """Bound password hashing work and attempts in this single web process."""

    def __init__(self):
        self.attempts = {}
        self.total = deque()
        self.lock = threading.Lock()
        self.hash_slots = threading.BoundedSemaphore(2)

    @contextmanager
    def attempt(self, client):
        now = time.monotonic()
        with self.lock:
            for key, times in list(self.attempts.items()):
                while times and times[0] <= now - 60:
                    times.popleft()
                if not times:
                    del self.attempts[key]
            while self.total and self.total[0] <= now - 60:
                self.total.popleft()
            times = self.attempts.get(client, deque())
            if len(times) >= 5:
                raise LoginThrottled(times[0] + 60 - now)
            if len(self.total) >= 60 or (
                client not in self.attempts and len(self.attempts) >= 2048
            ):
                raise LoginThrottled(60)
            if not self.hash_slots.acquire(blocking=False):
                raise LoginThrottled(1)
            times.append(now)
            self.total.append(now)
            self.attempts[client] = times
        try:
            yield
        finally:
            self.hash_slots.release()


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

    def password_status(self):
        record = self.read().get("password")
        return {
            "enabled": bool(record),
            "updated_at": record["updated_at"] if record else None,
            "can_configure": self.has_admin(),
            "min_length": PASSWORD_MIN_LENGTH,
            "max_length": PASSWORD_MAX_LENGTH,
        }

    @staticmethod
    def password_principal(record):
        return {
            "id": record["id"],
            "server": None,
            "label": "Administrator password",
            "method": "password",
        }

    def set_password(self, password, *, expected_id=...):
        validate_password(password)
        salt = secrets.token_hex(16)
        record = {
            "id": "password-" + secrets.token_hex(12),
            "algorithm": "scrypt-n32768-r8-p3",
            "salt": salt,
            "digest": password_digest(password, salt),
            "updated_at": time.time(),
        }
        with self.edit() as data:
            if not any(item["server"] is None for item in data["tokens"]):
                raise ValueError("请先创建管理员访问令牌，再启用密码登录")
            current_id = (data.get("password") or {}).get("id")
            if expected_id is not ... and current_id != expected_id:
                raise ValueError("密码设置已变化，请重新登录后重试")
            data["password"] = record
        return self.password_principal(record)

    def disable_password(self, *, expected_id=...):
        with self.edit() as data:
            if not any(item["server"] is None for item in data["tokens"]):
                raise ValueError("请先创建管理员访问令牌")
            current_id = (data.get("password") or {}).get("id")
            if expected_id is not ... and current_id != expected_id:
                raise ValueError("密码设置已变化，请重新登录后重试")
            data["password"] = None

    def authenticate_password(self, password):
        record = self.read().get("password")
        if (
            not record
            or not isinstance(password, str)
            or not 1 <= len(password) <= PASSWORD_MAX_LENGTH
        ):
            return None
        if record.get("algorithm") != "scrypt-n32768-r8-p3":
            return None
        try:
            digest = password_digest(password, record["salt"])
        except UnicodeEncodeError:
            return None
        if hmac.compare_digest(digest, record["digest"]):
            return self.password_principal(record)
        return None

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

    def session(self, principal, *, remember=False):
        expires = int(time.time()) + (REMEMBER_SECONDS if remember else SESSION_SECONDS)
        nonce = ("r" if remember else "s") + secrets.token_hex(12)
        message = f"{principal['id']}.{expires}.{nonce}"
        signature = hmac.new(
            self.read()["session_key"].encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return message + "." + signature

    @staticmethod
    def session_remembered(cookie):
        parts = (cookie or "").split(".")
        return len(parts) == 4 and parts[2].startswith("r")

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
        password = data.get("password")
        if password and password["id"] == token_id:
            return self.password_principal(password)
        return next(
            (
                self.public(item)
                for item in data["tokens"]
                if item["id"] == token_id and item["server"] is None
            ),
            None,
        )
