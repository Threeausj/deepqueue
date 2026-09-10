"""Isolated project files and HTTP/WebSocket app for workspace integration tests."""

import contextlib
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


def add_workspace(fixture, root):
    root.mkdir(exist_ok=True)
    (root / "notes").mkdir(exist_ok=True)
    (root / "README.md").write_text(
        "# 工作区预览验证\n\n验证精度 **0.93**。\n\n| 轮次 | 精度 |\n| --- | --- |\n| 1 | 0.93 |\n"
    )
    (root / "notes" / "train.py").write_text("accuracy = 0.93\nprint(accuracy)\n")
    (root / "page.html").write_text(
        "<!doctype html><html><body><h1>项目 HTML 文件</h1><p>文件预览已加载</p></body></html>"
    )
    (root / "chart.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">'
        '<rect width="186" height="40" y="20" fill="#556bd7"/>'
        '<text x="10" y="46" fill="white">Accuracy 0.93</text></svg>'
    )
    fixture.add_thread("workspace-tools", "工作区工具验证", cwd=str(root))
    fixture.add_thread("workspace-other", "缓存切换验证", cwd=str(root))
    fixture.skills = [
        {
            "name": "experiment-review",
            "path": str(root / ".agents/skills/experiment-review/SKILL.md"),
            "enabled": True,
            "description": "分析实验精度变化（协议夹具）",
        },
        {
            "name": "plot-results",
            "path": str(root / ".agents/plugins/lab/skills/plot-results/SKILL.md"),
            "enabled": True,
            "pluginId": "lab@fixture",
            "description": "绘制实验结果（协议夹具）",
        },
    ]
    fixture.plugins = [
        {
            "id": "lab@fixture",
            "name": "lab",
            "installed": True,
            "enabled": True,
            "interface": {"displayName": "实验工具", "shortDescription": "项目插件（协议夹具）"},
        }
    ]
    for tid, text in [
        ("workspace-tools", "缓存中的实验结论：验证精度提高到 0.93。"),
        ("workspace-other", "第二个任务的独立记录。"),
    ]:
        fixture.threads[tid]["turns"] = [
            {
                "id": "cached-turn",
                "status": "completed",
                "items": [{"id": "cached-answer", "type": "agentMessage", "text": text}],
            }
        ]
    return {"thread_id": "workspace-tools", "other_thread_id": "workspace-other", "root": str(root)}


def preview_app():
    app = FastAPI()

    @app.get("/")
    def index():
        return HTMLResponse(
            """<!doctype html><html><head><link rel="stylesheet" href="/assets/app.css"></head>
<body><h1>实时项目服务</h1><p id="metrics">加载指标…</p><p id="socket">连接中…</p>
<p id="isolated"></p><script src="/app.js"></script></body></html>"""
        )

    @app.get("/assets/app.css")
    def style():
        return Response(
            "body{font:16px system-ui;padding:24px;background:#f4f7fb;color:#283650}"
            "h1{color:rgb(70,90,200)}",
            media_type="text/css",
        )

    @app.get("/app.js")
    def script():
        return Response(
            """fetch('/api/metrics').then(r=>r.json()).then(v=>
document.querySelector('#metrics').textContent='Accuracy '+v.accuracy);
const ws=new WebSocket(location.origin.replace(/^http/,'ws')+'/socket');
ws.onopen=()=>ws.send('preview-ready');ws.onmessage=e=>document.querySelector('#socket').textContent=e.data;
try { parent.document.body; document.querySelector('#isolated').textContent='same origin'; }
catch { document.querySelector('#isolated').textContent='独立预览'; }
""",
            media_type="text/javascript",
        )

    @app.get("/api/metrics")
    def metrics():
        return {"accuracy": 0.93}

    @app.api_route("/api/echo", methods=["GET", "POST"])
    async def echo(request: Request):
        return {
            "headers": dict(request.headers),
            "body": (await request.body()).decode(),
            "query": request.url.query,
        }

    @app.get("/redirect")
    def redirect(request: Request):
        return RedirectResponse(f"http://127.0.0.1:{request.url.port}/api/metrics?from=redirect")

    @app.get("/cookies")
    def cookies():
        response = JSONResponse({"ok": True})
        response.set_cookie("project_session", "fixture", domain="localhost")
        response.set_cookie("deepqueue_session", "must-not-overwrite")
        return response

    @app.websocket("/socket")
    async def ws(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                event = await socket.receive()
                if event["type"] == "websocket.disconnect":
                    return
                if event.get("bytes") is not None:
                    await socket.send_bytes(event["bytes"])
                else:
                    await socket.send_text("WebSocket: " + event["text"])
        except Exception:
            pass

    return app


@contextlib.contextmanager
def preview_service():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(preview_app(), log_level="error", loop="asyncio"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("Preview fixture did not start")
            time.sleep(0.01)
        yield port
    finally:
        server.should_exit = True
        worker.join(5)
        listener.close()
