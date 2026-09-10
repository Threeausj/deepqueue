from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import posixpath
import shlex
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import paramiko

from . import worker
from .config import Secrets, private_write
from .models import Server


def fingerprint(key):
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    return "SHA256:" + digest


def host_key(host, port=22):
    with socket.create_connection((host, port), timeout=10) as sock:
        with paramiko.Transport(sock) as connection:
            connection.start_client(timeout=10)
            return connection.get_remote_server_key()


def trust_host(home, host, port, expected):
    key = host_key(host, port)
    observed = fingerprint(key)
    if observed != expected:
        raise ValueError(f"Host key mismatch: observed {observed}")
    path = home / "known_hosts"
    with (home / "known_hosts.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        keys = paramiko.HostKeys(str(path)) if path.exists() else paramiko.HostKeys()
        hostname = host if port == 22 else f"[{host}]:{port}"
        existing = keys.lookup(hostname)
        if existing and not keys.check(hostname, key):
            raise ValueError(
                "Host key changed; inspect the existing known_hosts entry before replacing it"
            )
        keys.add(hostname, key.get_name(), key)
        temporary = path.with_name("known_hosts.tmp-" + uuid.uuid4().hex)
        try:
            private_write(temporary, "")
            keys.save(str(temporary))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"host": host, "port": port, "fingerprint": observed, "known_hosts": str(path)}


def read_channel(channel, timeout=40):
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while channel.recv_ready():
            output.extend(channel.recv(65536))
            if len(output) > 8 * 1024 * 1024:
                raise ValueError("Worker response exceeded the 8 MiB transport limit")
        while channel.recv_stderr_ready():
            chunk = channel.recv_stderr(65536)
            errors.extend(chunk[: max(0, 65536 - len(errors))])
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            return (
                output.decode("utf-8", "replace"),
                errors.decode("utf-8", "replace"),
                channel.recv_exit_status(),
            )
        time.sleep(0.01)
    raise TimeoutError("SSH worker response timed out")


class Transport:
    def __init__(self, home: Path, server: Server):
        self.home = home
        self.server = server
        self.source = Path(worker.__file__).read_bytes()
        self.worker_name = "worker-" + hashlib.sha256(self.source).hexdigest()[:16] + ".py"

    def terminal(self, run_id):
        result = self.call("terminal", run_id=run_id)
        if result.get("attach_command") and self.server.kind == "ssh":
            args = [
                "ssh",
                "-t",
                "-p",
                str(self.server.port),
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self.home / 'known_hosts'}",
            ]
            if self.server.key_file:
                args.extend(["-i", str(Path(self.server.key_file).expanduser())])
            args.extend([f"{self.server.username}@{self.server.host}", result["attach_command"]])
            result["attach_command"] = shlex.join(args)
        return result

    @contextmanager
    def ssh(self):
        server = self.server
        secrets = Secrets(self.home)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if (self.home / "known_hosts").exists():
            client.load_host_keys(str(self.home / "known_hosts"))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=server.host,
                port=server.port,
                username=server.username,
                key_filename=str(Path(server.key_file).expanduser()) if server.key_file else None,
                password=secrets.get(server.password_ref),
                passphrase=secrets.get(server.passphrase_ref),
                look_for_keys=not server.password_ref and not server.key_file,
                allow_agent=not server.password_ref and not server.key_file,
                timeout=10,
                auth_timeout=10,
                banner_timeout=10,
            )
            yield client
        finally:
            client.close()

    def _remote_worker(self, sftp):
        root = self.server.worker_root
        remote_home = sftp.normalize(".")
        if root == "~":
            root = remote_home
        elif root.startswith("~/"):
            root = posixpath.join(remote_home, root[2:])
        elif not root.startswith("/"):
            root = posixpath.join(remote_home, root)
        root = posixpath.normpath(root)
        current = "/"
        for part in root.split("/"):
            if not part:
                continue
            current = posixpath.join(current, part)
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current, mode=0o700)
        script = posixpath.join(root, self.worker_name)
        try:
            sftp.stat(script)
        except FileNotFoundError:
            temporary = script + ".tmp-" + uuid.uuid4().hex
            with sftp.open(temporary, "wb") as f:
                f.write(self.source)
            sftp.chmod(temporary, 0o700)
            try:
                sftp.posix_rename(temporary, script)
            except OSError:
                try:
                    sftp.rename(temporary, script)
                except OSError:
                    sftp.stat(script)
                    sftp.remove(temporary)
        return root, script

    def call(self, action: str, **params):
        request = {"action": action, **params}
        if self.server.kind == "local":
            root = Path(self.server.worker_root).expanduser().resolve()
            script = root / self.worker_name
            if not script.exists():
                private_write(script, self.source)
            request["root"] = str(root)
            result = subprocess.run(
                [self.server.python, str(script)],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=40,
            )
            output, error, code = result.stdout, result.stderr, result.returncode
        else:
            with self.ssh() as client:
                with client.open_sftp() as sftp:
                    root, script = self._remote_worker(sftp)
                request["root"] = root
                command = shlex.join([self.server.python, script])
                stdin, stdout, stderr = client.exec_command(command, timeout=40)
                stdin.write(json.dumps(request).encode())
                stdin.flush()
                stdin.channel.shutdown_write()
                output, error, code = read_channel(stdout.channel)
        try:
            response = json.loads(output)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Worker returned invalid JSON (exit {code}): {error[:500]}"
            ) from exc
        if code:
            raise RuntimeError(response.get("error", f"Worker exited {code}"))
        return response
