"""A WebSocket over the official SSH stdio proxy; no remote public listener."""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import shlex
import socket
import subprocess

from websockets.asyncio.client import connect

from .transport import Transport, read_channel


def ssh_codex_command(args):
    command = "exec " + shlex.join(args)
    if posixpath.isabs(args[0]):
        # npm's launcher uses /usr/bin/env node. Keep the configured bin directory
        # (not the symlink target) so nvm's sibling node is available over SSH.
        directory = shlex.quote(posixpath.dirname(args[0]))
        command = f'PATH={directory}:"${{PATH:-/usr/local/bin:/usr/bin:/bin}}" {command}'
    return command


class SSHCodexSocket:
    def __init__(self, home, server, stderr):
        self.transport = Transport(home, server)
        self.server = server
        self.stderr = stderr
        self.context = None
        self.channel = None
        self.peer = None
        self.endpoint = None
        self.tasks = []
        self.ws = None

    def _open(self):
        context = self.transport.ssh()
        client = context.__enter__()
        try:
            client.get_transport().set_keepalive(15)
            args = [self.server.codex.executable, "app-server", "proxy"]
            if self.server.codex.socket != "auto":
                args += ["--sock", self.server.codex.socket]
            channel = client.get_transport().open_session(timeout=10)
            channel.settimeout(10)
            channel.exec_command(ssh_codex_command(args))
            return context, channel
        except BaseException:
            context.__exit__(None, None, None)
            raise

    async def open(self):
        opening = asyncio.create_task(asyncio.to_thread(self._open))
        try:
            self.context, self.channel = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # A cancelled HTTP request must not leak an SSH connection still authenticating.
            with contextlib.suppress(Exception):
                self.context, self.channel = await opening
                await self.close()
            raise
        try:
            self.endpoint, self.peer = socket.socketpair()
            self.endpoint.setblocking(False)
            self.peer.setblocking(False)
            self.tasks = [
                asyncio.create_task(self._upload()),
                asyncio.create_task(self._download()),
            ]
            self.ws = await connect(
                "ws://localhost/rpc",
                sock=self.endpoint,
                compression=None,
                open_timeout=10,
                close_timeout=2,
                max_size=64 * 1024 * 1024,
            )
            return self.ws
        except BaseException:
            await self.close()
            raise

    async def _upload(self):
        loop = asyncio.get_running_loop()
        try:
            while data := await loop.sock_recv(self.peer, 65536):
                await asyncio.to_thread(self.channel.sendall, data)
        except (OSError, EOFError):
            pass
        finally:
            self.channel.close()

    async def _download(self):
        loop = asyncio.get_running_loop()
        try:
            while True:
                if self.channel.recv_stderr_ready():
                    self.stderr.write(self.channel.recv_stderr(65536))
                    self.stderr.flush()
                if self.channel.recv_ready():
                    data = self.channel.recv(65536)
                    if not data:
                        break
                    await loop.sock_sendall(self.peer, data)
                elif self.channel.closed or self.channel.exit_status_ready():
                    break
                else:
                    await asyncio.sleep(0.01)
        except (OSError, EOFError):
            pass
        finally:
            self.peer.close()

    async def close(self):
        if self.channel:
            self.channel.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.ws:
            await self.ws.close()
        for endpoint in (self.peer, self.endpoint):
            if endpoint:
                endpoint.close()
        if self.context:
            self.context.__exit__(None, None, None)
            self.context = None


def start_daemon(home, server):
    """Explicit user action. Never restart/stop an existing shared Codex daemon."""
    if server.codex.socket != "auto":
        raise ValueError("Start a custom socket using Codex on the target server first")
    args = [server.codex.executable, "app-server", "daemon", "start"]
    if server.kind == "local":
        env = os.environ.copy()
        if os.path.isabs(args[0]):
            env["PATH"] = os.path.dirname(args[0]) + os.pathsep + (env.get("PATH") or os.defpath)
        result = subprocess.run(args, capture_output=True, text=True, timeout=50, env=env)
        output, error, code = result.stdout, result.stderr, result.returncode
    else:
        with Transport(home, server).ssh() as client:
            _, stdout, _ = client.exec_command(ssh_codex_command(args), timeout=50)
            output, error, code = read_channel(stdout.channel, timeout=50)
    if code:
        raise RuntimeError((error or output or f"Codex exited {code}")[-4000:])
