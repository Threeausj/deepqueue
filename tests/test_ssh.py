"""Real loopback SSH/SFTP transport tests with isolated fixture credentials."""

import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import paramiko
import pytest

from deepqueue.config import Secrets
from deepqueue.models import Server
from deepqueue.transport import Transport, fingerprint, trust_host


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = server.root

    def path(self, name):
        path = (self.root / name).resolve()
        path.relative_to(self.root)
        return path

    def canonicalize(self, path):
        return str(self.path(path))

    def stat(self, path):
        try:
            candidate = Path(path)
            target = (
                candidate
                if candidate.is_absolute() and self.root.is_relative_to(candidate)
                else self.path(path)
            )
            return paramiko.SFTPAttributes.from_stat(target.stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def mkdir(self, path, attr):
        self.path(path).mkdir(mode=attr.st_mode or 0o700)
        return paramiko.SFTP_OK

    def chattr(self, path, attr):
        if attr.st_mode is not None:
            self.path(path).chmod(attr.st_mode)
        return paramiko.SFTP_OK

    def open(self, path, flags, attr):
        try:
            fd = os.open(self.path(path), flags, attr.st_mode or 0o600)
            mode = "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb"
            file = os.fdopen(fd, mode)
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = file
            handle.writefile = file
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, old, new):
        self.path(old).rename(self.path(new))
        return paramiko.SFTP_OK

    posix_rename = rename

    def remove(self, path):
        self.path(path).unlink()
        return paramiko.SFTP_OK


class Auth(paramiko.ServerInterface):
    def __init__(self, root, public_key):
        self.root = root
        self.public_key = public_key

    def check_auth_password(self, username, password):
        return (
            paramiko.AUTH_SUCCESSFUL
            if username == "fixture" and password == "fixture-password"
            else paramiko.AUTH_FAILED
        )

    def check_auth_publickey(self, username, key):
        return (
            paramiko.AUTH_SUCCESSFUL
            if username == "fixture" and key == self.public_key
            else paramiko.AUTH_FAILED
        )

    def get_allowed_auths(self, _):
        return "password,publickey"

    def check_channel_request(self, kind, _):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )

    def check_channel_exec_request(self, channel, command):
        arguments = shlex.split(command.decode())
        if len(arguments) != 2 or arguments[0] != sys.executable:
            return False
        try:
            Path(arguments[1]).resolve().relative_to(self.root)
        except ValueError:
            return False

        def execute():
            data = bytearray()
            while chunk := channel.recv(65536):
                data.extend(chunk)
            process = subprocess.run(arguments, input=bytes(data), capture_output=True, timeout=40)
            channel.sendall(process.stdout)
            if process.stderr:
                channel.sendall_stderr(process.stderr)
            channel.send_exit_status(process.returncode)
            channel.close()

        threading.Thread(target=execute, daemon=True).start()
        return True


@contextmanager
def loopback_ssh(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    host_key = paramiko.RSAKey.generate(2048)
    user_key = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "fixture-key"
    user_key.write_private_key_file(str(key_path), password="fixture-passphrase")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    stop = threading.Event()
    connections = []

    def handle(sock):
        connection = paramiko.Transport(sock)
        connections.append(connection)
        connection.add_server_key(host_key)
        connection.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
        try:
            connection.start_server(server=Auth(root, user_key))
            while connection.is_active() and not stop.wait(0.1):
                pass
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            connection.close()

    def serve():
        while not stop.is_set():
            try:
                sock, _ = listener.accept()
                threading.Thread(target=handle, args=(sock,), daemon=True).start()
            except TimeoutError:
                pass
            except OSError:
                break

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield {
            "port": listener.getsockname()[1],
            "fingerprint": fingerprint(host_key),
            "key": key_path,
            "root": root,
        }
    finally:
        stop.set()
        listener.close()
        for connection in connections:
            connection.close()
        thread.join(timeout=2)


@pytest.fixture
def ssh_server(tmp_path):
    with loopback_ssh(tmp_path) as fixture:
        yield fixture


@pytest.mark.parametrize("authentication", ["password", "key"])
def test_real_ssh_password_key_and_sftp_worker(db, ssh_server, authentication):
    fixture = ssh_server
    trust_host(db.home, "127.0.0.1", fixture["port"], fixture["fingerprint"])
    vault = Secrets(db.home)
    auth = (
        {"password_ref": vault.put("fixture-password")}
        if authentication == "password"
        else {"key_file": str(fixture["key"]), "passphrase_ref": vault.put("fixture-passphrase")}
    )
    server = Server(
        name="loopback",
        kind="ssh",
        host="127.0.0.1",
        port=fixture["port"],
        username="fixture",
        python=sys.executable,
        **auth,
    )
    client = Transport(db.home, server)
    run_id = uuid.uuid4().hex
    result = client.call(
        "launch",
        run_id=run_id,
        spec={
            "cwd": str(fixture["root"]),
            "command": "printf 'ssh-verified'",
            "env": {},
            "gpu_uuids": [],
            "timeout_seconds": 0,
        },
    )
    assert result["status"] == "starting"
    for _ in range(30):
        result = client.call("inspect", run_id=run_id)
        if result["status"] == "succeeded":
            break
        time.sleep(0.1)
    assert result["status"] == "succeeded"
    assert client.call("logs", run_id=run_id)["text"] == "ssh-verified"
    assert (
        client.call(
            "launch",
            run_id=run_id,
            spec={
                "cwd": str(fixture["root"]),
                "command": "printf 'ssh-verified'",
                "env": {},
                "gpu_uuids": [],
                "timeout_seconds": 0,
            },
        )["status"]
        == "succeeded"
    )


def test_unknown_host_key_and_wrong_fingerprint_rejected(db, ssh_server):
    server = Server(
        name="untrusted",
        kind="ssh",
        host="127.0.0.1",
        port=ssh_server["port"],
        username="fixture",
        password_ref=Secrets(db.home).put("fixture-password"),
    )
    with pytest.raises(paramiko.SSHException, match="known_hosts"):
        Transport(db.home, server).call("probe")
    with pytest.raises(ValueError, match="mismatch"):
        trust_host(db.home, "127.0.0.1", ssh_server["port"], "SHA256:wrong")
