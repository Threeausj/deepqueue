from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

from .models import Settings


def state_home(value: str | Path | None = None) -> Path:
    return (
        Path(value or os.environ.get("DEEPQUEUE_HOME", "~/.local/share/deepqueue"))
        .expanduser()
        .resolve()
    )


def private_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(home: Path) -> Settings:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = home / "config.json"
    if not config_path.exists():
        private_write(config_path, Settings().model_dump_json(indent=2) + "\n")
    return load_settings(home)


def load_settings(home: Path) -> Settings:
    path = home / "config.json"
    if not path.exists():
        raise ValueError(f"Run deepqueue --home {home} init first")
    return Settings.model_validate_json(path.read_text())


def update_settings(home: Path, changes: dict) -> Settings:
    with (home / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_settings(home)
        updated = Settings.model_validate({**current.model_dump(), **changes})
        private_write(home / "config.json", updated.model_dump_json(indent=2) + "\n")
    return updated


class Secrets:
    def __init__(self, home: Path):
        self.home = home

    def _cipher(self) -> Fernet:
        key = self.home / "secrets.key"
        if not key.exists():
            try:
                with os.fdopen(
                    os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
                ) as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
        return Fernet(key.read_bytes())

    def put(self, value: str) -> str:
        name = os.urandom(16).hex()
        private_write(self.home / "secrets" / name, self._cipher().encrypt(value.encode()))
        return "vault:" + name

    def get(self, reference: str | None) -> str | None:
        if reference is None:
            return None
        if reference.startswith("env:"):
            name = reference[4:]
            if name not in os.environ:
                raise ValueError(f"Missing credential environment variable: {name}")
            return os.environ[name]
        if reference.startswith("vault:"):
            name = reference[6:]
            if len(name) != 32 or any(c not in "0123456789abcdef" for c in name):
                raise ValueError("Invalid vault reference")
            return self._cipher().decrypt((self.home / "secrets" / name).read_bytes()).decode()
        raise ValueError("Credential references must use env:NAME or vault:ID")


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
