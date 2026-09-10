"""Server metadata and SSH configuration shared by the dashboard and CLI."""

from typing import Literal

from pydantic import Field

from .config import Secrets
from .db import Database
from .models import Model, Server
from .transport import trust_host


class ServerUpdate(Model):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=100)
    max_running: int | None = Field(default=None, ge=1, le=1000)
    authentication: Literal["keep", "key", "password"] = "keep"
    key_file: str | None = None
    password: str | None = None
    passphrase: str | None = None
    fingerprint: str | None = None


def update_server(home, name, update: ServerUpdate):
    db = Database(home)
    current = Server.model_validate(db.server(name)["config"])
    fields = {"host", "port", "username", "key_file", "password_ref", "passphrase_ref"}
    expected = current.model_dump(include=fields)
    changes = update.model_dump(
        exclude_unset=True,
        exclude={"authentication", "key_file", "password", "passphrase", "fingerprint"},
    )
    if update.authentication == "keep":
        if any(getattr(update, key) is not None for key in ("key_file", "password", "passphrase")):
            raise ValueError("修改凭据时请选择 SSH 密码或密钥认证")
    else:
        if current.kind != "ssh":
            raise ValueError("本机服务器不支持 SSH 连接设置")
        if update.authentication == "password":
            if not update.password or update.key_file or update.passphrase:
                raise ValueError("请填写 SSH 密码，并清空密钥设置")
            changes.update(key_file=None, passphrase_ref=None, password_ref="new-password")
        else:
            if update.password:
                raise ValueError("密钥认证不能同时设置 SSH 密码")
            changes.update(
                key_file=update.key_file or None,
                password_ref=None,
                passphrase_ref="new-passphrase" if update.passphrase else None,
            )
    candidate = Server.model_validate({**current.model_dump(), **changes})
    db.update_server(name, changes, expected_connection=expected, check_only=True)
    address_changed = (current.host, current.port) != (candidate.host, candidate.port)
    if address_changed:
        if current.kind != "ssh":
            raise ValueError("本机服务器不支持 SSH 连接设置")
        if not update.fingerprint:
            raise ValueError("修改主机地址或端口后，请重新核对并确认主机指纹")
        trust_host(home, candidate.host, candidate.port, update.fingerprint)
    vault = Secrets(home)
    created_secret = None
    if update.authentication == "password":
        created_secret = changes["password_ref"] = vault.put(update.password)
    elif update.authentication == "key" and update.passphrase:
        created_secret = changes["passphrase_ref"] = vault.put(update.passphrase)
    try:
        db.update_server(name, changes, expected_connection=expected)
    except Exception:
        if created_secret:
            (home / "secrets" / created_secret.removeprefix("vault:")).unlink(missing_ok=True)
        raise
    return db.server(name)
