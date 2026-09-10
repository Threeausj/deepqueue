"""Full-origin HTTP/WebSocket preview of an execution server's loopback service."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import io
import re
import secrets
import socket
import time
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field
from websockets.asyncio.client import connect

from .access import COOKIE, Access
from .codex_transport import SSHCodexSocket
from .config import load_settings
from .models import Model
from .transport import Transport

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
MAX_BODY = 16 * 1024 * 1024


class PreviewTarget(Model):
    port: int = Field(ge=1, le=65535)


class PreviewReference(Model):
    id: str = Field(pattern=r"^[a-f0-9]{48}$")


def credential(request):
    authorization = request.headers.get("authorization", "")
    return (
        ("token", authorization[7:])
        if authorization.startswith("Bearer ")
        else ("session", request.cookies.get(COOKIE))
    )


def proxy_headers(headers, origin, upstream):
    excluded = HOP_HEADERS | {v.strip().lower() for v in headers.get("connection", "").split(",")}
    result = []
    for name, value in headers.items():
        key = name.lower()
        if key in excluded or key.startswith(("sec-websocket-", "x-forwarded-", "x-deepqueue")):
            continue
        if key == "authorization" and value.startswith("Bearer dq_"):
            continue
        if key == "cookie":
            value = "; ".join(
                part.strip()
                for part in value.split(";")
                if not part.strip().split("=", 1)[0].startswith("deepqueue")
            )
        if key in ("origin", "referer") and value.startswith(origin):
            value = upstream + value[len(origin) :]
        result.append((name, value))
    return result


class ChannelSocket:
    """Socket file semantics for HTTP bodies that outlive Connection: close."""

    def __init__(self, channel):
        self.channel = channel
        self.file_count = 0
        self.closed = False

    def sendall(self, data):
        self.channel.sendall(data)

    def close(self):
        self.closed = True
        if not self.file_count:
            self.channel.close()

    def makefile(self, mode):
        assert mode == "rb"
        self.file_count += 1
        owner = self

        class Reader(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, buffer):
                data = owner.channel.recv(len(buffer))
                buffer[: len(data)] = data
                return len(data)

            def close(self):
                if not self.closed:
                    owner.file_count -= 1
                    if owner.closed and not owner.file_count:
                        owner.channel.close()
                super().close()

        return io.BufferedReader(Reader())


class PreviewSocket(SSHCodexSocket):
    def __init__(self, home, server, port):
        super().__init__(home, server, io.BytesIO())
        self.port = port

    def _open(self):
        context = self.transport.ssh()
        client = context.__enter__()
        try:
            client.get_transport().set_keepalive(15)
            channel = client.get_transport().open_channel(
                "direct-tcpip", ("127.0.0.1", self.port), ("127.0.0.1", 0), timeout=10
            )
            return context, channel
        except BaseException:
            context.__exit__(None, None, None)
            raise

    async def open_endpoint(self):
        opening = asyncio.create_task(asyncio.to_thread(self._open))
        try:
            self.context, self.channel = await asyncio.shield(opening)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self.context, self.channel = await opening
                await self.close()
            raise
        self.endpoint, self.peer = socket.socketpair()
        self.endpoint.setblocking(False)
        self.peer.setblocking(False)
        self.tasks = [asyncio.create_task(self._upload()), asyncio.create_task(self._download())]
        return self.endpoint


class Preview:
    def __init__(self, manager, session, thread_id, port, origin, auth):
        self.manager, self.session, self.thread_id = manager, session, thread_id
        self.port, self.origin, self.auth = port, origin, auth
        self.generation = session.generation
        self.expires = time.monotonic() + 1800
        self.context = self.client = None
        self.lock = asyncio.Lock()
        self.connections = set()
        self.bridges = set()
        self.closed = False

    def authorized(self):
        if (
            self.closed
            or time.monotonic() >= self.expires
            or self.session.state != "connected"
            or self.session.generation != self.generation
        ):
            return False
        access = Access(self.manager.home)
        if not access.enabled() and not load_settings(self.manager.home).public_url:
            return True
        principal = (
            access.authenticate(self.auth[1])
            if self.auth[0] == "token"
            else access.authenticate_session(self.auth[1])
        )
        return principal is not None and principal["server"] is None

    def _ssh_client(self):
        if self.client and self.client.get_transport().is_active():
            return self.client
        if self.context:
            self.context.__exit__(None, None, None)
        self.context = Transport(self.manager.home, self.session.server).ssh()
        self.client = self.context.__enter__()
        self.client.get_transport().set_keepalive(15)
        return self.client

    def _channel(self):
        if self.session.server.kind == "ssh":
            client = self._ssh_client()
            return client.get_transport().open_channel(
                "direct-tcpip", ("127.0.0.1", self.port), ("127.0.0.1", 0), timeout=10
            )
        return socket.create_connection(("127.0.0.1", self.port), 10)

    async def channel(self):
        async with self.lock:
            if not self.authorized():
                raise HTTPException(401, "预览已过期，请回到工作区重新打开")
            if len(self.connections) + len(self.bridges) >= 48:
                raise HTTPException(429, "预览连接过多，请稍后重试")
            opening = asyncio.create_task(asyncio.to_thread(self._channel))
            try:
                channel = await asyncio.shield(opening)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    (await opening).close()
                raise
            if not self.authorized():
                channel.close()
                raise HTTPException(401, "预览已关闭")
            channel.settimeout(30)
            self.connections.add(channel)
            return channel

    async def close(self):
        self.closed = True
        for channel in list(self.connections):
            channel.close()
        self.connections.clear()
        await asyncio.gather(*(bridge.close() for bridge in list(self.bridges)))
        self.bridges.clear()
        async with self.lock:
            if self.context:
                await asyncio.to_thread(self.context.__exit__, None, None, None)
                self.context = self.client = None

    async def http(self, request):
        if not self.authorized():
            return JSONResponse({"detail": "预览已过期，请回到工作区重新打开"}, 401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({"detail": "预览请求超过 16 MiB"}, 413)
        channel = None
        connection = None
        try:
            channel = await self.channel()
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            connection.sock = (
                ChannelSocket(channel) if self.session.server.kind == "ssh" else channel
            )
            path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
            if request.url.query:
                path += "?" + request.url.query
            target = f"http://127.0.0.1:{self.port}"
            headers = proxy_headers(request.headers, self.origin, target)

            def fetch():
                connection.putrequest(
                    request.method, path, skip_host=True, skip_accept_encoding=True
                )
                connection.putheader("Host", f"127.0.0.1:{self.port}")
                connection.putheader("Connection", "close")
                for name, value in headers:
                    connection.putheader(name, value)
                if body or request.method in ("POST", "PUT", "PATCH"):
                    connection.putheader("Content-Length", str(len(body)))
                connection.endheaders(body if body else None)
                return connection.getresponse()

            response = await asyncio.to_thread(fetch)
            excluded = HOP_HEADERS | {
                v.strip().lower() for v in (response.getheader("connection") or "").split(",")
            }
            outgoing = []
            for name, value in response.getheaders():
                key = name.lower()
                if key in excluded or key == "referrer-policy":
                    continue
                if key == "location":
                    parsed = urlsplit(value)
                    if (
                        parsed.hostname in ("127.0.0.1", "localhost", "::1")
                        and (parsed.port or 80) == self.port
                    ):
                        base = urlsplit(self.origin)
                        value = urlunsplit(
                            (base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment)
                        )
                if key == "set-cookie":
                    if value.split("=", 1)[0].lower().startswith("deepqueue"):
                        continue
                    value = re.sub(r";\s*domain=[^;]+", "", value, flags=re.I)
                outgoing.append((key.encode("latin-1"), value.encode("latin-1")))
            outgoing.extend(
                [(b"referrer-policy", b"no-referrer"), (b"x-content-type-options", b"nosniff")]
            )

            async def stream():
                try:
                    while self.authorized():
                        chunk = await asyncio.to_thread(response.read1, 65536)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    channel.close()
                    response.close()
                    connection.close()
                    self.connections.discard(channel)

            result = StreamingResponse(stream(), status_code=response.status)
            result.raw_headers = outgoing
            return result
        except BaseException as exc:
            if connection:
                connection.close()
            if channel:
                channel.close()
                self.connections.discard(channel)
            if not isinstance(exc, Exception):
                raise
            status = exc.status_code if isinstance(exc, HTTPException) else 502
            detail = str(getattr(exc, "detail", str(exc)))[:500]
            return JSONResponse(
                {"detail": f"无法连接此服务器的 127.0.0.1:{self.port}：{detail}"},
                status,
            )

    async def websocket(self, socket):
        if not self.authorized():
            await socket.close(code=4401)
            return
        bridge = None
        endpoint = None
        failed = False
        try:
            if self.session.server.kind == "ssh":
                if len(self.bridges) + len(self.connections) >= 48:
                    await socket.close(code=4429)
                    return
                bridge = PreviewSocket(self.manager.home, self.session.server, self.port)
                self.bridges.add(bridge)
                endpoint = await bridge.open_endpoint()
                if not self.authorized():
                    return
            else:
                endpoint = await self.channel()
                endpoint.setblocking(False)
            path = socket.scope.get("raw_path", socket.url.path.encode()).decode("ascii")
            if socket.url.query:
                path += "?" + socket.url.query
            headers = proxy_headers(socket.headers, self.origin, f"http://127.0.0.1:{self.port}")
            protocols = [
                v.strip()
                for v in socket.headers.get("sec-websocket-protocol", "").split(",")
                if v.strip()
            ]
            async with connect(
                f"ws://127.0.0.1:{self.port}{path}",
                sock=endpoint,
                proxy=None,
                additional_headers=headers,
                subprotocols=protocols or None,
                max_size=16 * 1024 * 1024,
                open_timeout=10,
                close_timeout=2,
            ) as upstream:
                await socket.accept(subprotocol=upstream.subprotocol)

                async def download():
                    async for data in upstream:
                        if isinstance(data, bytes):
                            await socket.send_bytes(data)
                        else:
                            await socket.send_text(data)

                async def upload():
                    while True:
                        event = await socket.receive()
                        if event["type"] == "websocket.disconnect":
                            break
                        await upstream.send(
                            event.get("bytes") if event.get("bytes") is not None else event["text"]
                        )

                async def expiry():
                    while self.authorized():
                        await asyncio.sleep(15)

                tasks = [asyncio.create_task(action()) for action in (download, upload, expiry)]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            failed = True
        finally:
            with contextlib.suppress(Exception):
                await socket.close(code=4401 if not self.authorized() else 1011 if failed else 1000)
            if bridge:
                await bridge.close()
                self.bridges.discard(bridge)
            if endpoint:
                endpoint.close()
                self.connections.discard(endpoint)


class PreviewManager:
    def __init__(self, gateway):
        self.gateway, self.home = gateway, gateway.home
        self.previews = {}
        self.sweeper = None

    async def sweep(self):
        while True:
            for key, preview in list(self.previews.items()):
                if not preview.authorized():
                    self.previews.pop(key, None)
                    await preview.close()
            await asyncio.sleep(15)

    async def create(self, session, thread_id, data, request):
        session.require_client()
        await session.tools.thread(thread_id)
        if self.sweeper is None:
            self.sweeper = asyncio.create_task(self.sweep())
        if len(self.previews) >= 32:
            raise HTTPException(409, "预览数量已达上限，请先关闭一个预览")
        configured = load_settings(self.home).preview_origin
        if configured:
            origin = urlsplit(configured)
        elif request.url.hostname in ("localhost", "127.0.0.1", "::1"):
            port = f":{request.url.port}" if request.url.port else ""
            origin = urlsplit(f"{request.url.scheme}://localhost{port}")
        else:
            raise HTTPException(
                409, "请在通用设置的接入设置中配置独立预览域名，并将其子域名转发到同一服务。"
            )
        key = secrets.token_hex(24)
        netloc = f"p-{key}.{origin.hostname}" + (f":{origin.port}" if origin.port else "")
        preview_origin = f"{origin.scheme}://{netloc}"
        if preview_origin == f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(400, "预览必须使用独立地址")
        preview = Preview(self, session, thread_id, data.port, preview_origin, credential(request))
        # Fail promptly with a useful error if the service is not listening.
        try:
            probe = await preview.channel()
            probe.close()
            preview.connections.discard(probe)
        except Exception as exc:
            await preview.close()
            raise HTTPException(502, f"此服务器的 127.0.0.1:{data.port} 尚不可访问") from exc
        self.previews[key] = preview
        return {"id": key, "url": preview_origin + "/", "port": data.port, "expires_in": 1800}

    def checked(self, reference, session, thread_id):
        preview = self.previews.get(reference.id)
        if (
            not preview
            or preview.session is not session
            or preview.thread_id != thread_id
            or not preview.authorized()
        ):
            raise HTTPException(404, "预览不存在或连接已变化")
        return preview

    async def close_auth(self, auth):
        selected = [(key, p) for key, p in self.previews.items() if p.auth == auth]
        for key, _ in selected:
            self.previews.pop(key, None)
        await asyncio.gather(*(p.close() for _, p in selected), return_exceptions=True)

    async def close(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        await asyncio.gather(*(p.close() for p in self.previews.values()), return_exceptions=True)
        self.previews.clear()


class PreviewMiddleware:
    def __init__(self, app, manager):
        self.app, self.manager = app, manager

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers", []))
            host = headers.get(b"host", b"").decode("latin-1").lower()
            match = re.match(r"^p-([a-f0-9]{48})\.", host)
            if match:
                preview = self.manager.previews.get(match[1])
                if preview is None or host != urlsplit(preview.origin).netloc.lower():
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 4404})
                    else:
                        await JSONResponse({"detail": "预览已关闭或不存在"}, 404)(
                            scope, receive, send
                        )
                    return
                if scope["type"] == "websocket":
                    await preview.websocket(WebSocket(scope, receive, send))
                else:
                    response = await preview.http(Request(scope, receive))
                    await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
