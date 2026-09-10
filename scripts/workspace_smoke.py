"""Check workspace tools against native Codex using one isolated Luna task."""

import argparse
import asyncio
import base64
import contextlib
import json
from pathlib import Path

from deepqueue.agent import AppServer
from deepqueue.config import initialize
from deepqueue.db import Database
from deepqueue.gateway import AccessOptions, CodexGateway, Message, NewThread
from deepqueue.models import CodexConnection, Server
from deepqueue.workspace import ExtensionChoice, TerminalInput, TerminalResize, TerminalSize


async def run(home, terminal_permissions):
    if home.exists():
        raise ValueError("Choose a fresh test directory")
    initialize(home)
    skill = home / ".agents/skills/workspace-check/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: workspace-check\n"
        "description: Respond to the isolated workspace integration check.\n---\n\n"
        "Reply exactly DQ_WORKSPACE_SKILL_OK. Do not use tools or run experiments.\n"
    )
    (home / "README.md").write_text("# Workspace native check\n\nAccuracy fixture: 0.93\n")
    db = Database(home)
    db.initialize()
    db.add_server(Server(name="local", codex=CodexConnection(enabled=True, cwd=str(home))))
    gateway = CodexGateway(home, db)
    session = await gateway.session("local")
    task_id = None
    try:
        await session.connect()
        task_id = (
            await session.new_thread(
                NewThread(
                    cwd=str(home),
                    model="gpt-5.6-luna",
                    permissions=":read-only",
                    approval_policy="never",
                )
            )
        )["thread"]["id"]
        print("Created isolated native task", task_id, flush=True)
        catalog = await session.tools.extensions(task_id, force=True)
        selected = next(item for item in catalog["skills"] if item["name"] == "workspace-check")
        print(
            "Native skills/list found project skill; installed plugins:",
            len(catalog["plugins"]),
            flush=True,
        )
        events = asyncio.Queue(maxsize=256)
        session.subscribers.add(events)
        await session.send_message(
            task_id,
            Message(
                text="$workspace-check Reply DQ_WORKSPACE_SKILL_OK only. Do not use tools.",
                model="gpt-5.6-luna",
                effort="low",
                request_id="native-workspace-check",
                extensions=[ExtensionChoice(kind="skill", id=selected["path"])],
            ),
        )
        async with asyncio.timeout(180):
            while True:
                event = await events.get()
                if (
                    event.get("method") == "turn/completed"
                    and event["params"].get("threadId") == task_id
                ):
                    assert "DQ_WORKSPACE_SKILL_OK" in AppServer.final_text(
                        event["params"]["turn"]
                    ), event
                    break
        print("Luna responded using native skill input", flush=True)
        first = await session.open_thread(task_id, 40, force=True)
        assert (await session.open_thread(task_id, 40))["thread"]["turns"] == first["thread"][
            "turns"
        ]
        assert (await session.tools.file(task_id, "README.md"))["text"].startswith("# Workspace")
        if terminal_permissions != ":read-only":
            await session.update_access(task_id, AccessOptions(permissions=terminal_permissions))
        terminal = await session.tools.start_terminal(task_id, TerminalSize(cols=88, rows=24))
        async with asyncio.timeout(20):
            while session.tools.terminal_status(task_id)["state"] == "starting":
                await asyncio.sleep(0.1)
        state = session.tools.terminal_status(task_id)
        assert state["state"] == "running", state
        await session.tools.resize_terminal(
            task_id, TerminalResize(id=terminal["id"], cols=92, rows=25)
        )
        # Read-only harmless shell output; no training or persistent command side effects.
        await session.tools.write_terminal(
            task_id, TerminalInput(id=terminal["id"], data="printf '\\nDQ_PTY_%s\\n' VERIFIED\n")
        )
        async with asyncio.timeout(15):
            while b"DQ_PTY_VERIFIED" not in base64.b64decode(
                session.tools.terminal_status(task_id)["output_base64"]
            ):
                await asyncio.sleep(0.1)
        await session.tools.stop_terminal(task_id, terminal["id"])
        await asyncio.wait_for(session.tools.terminals[task_id]["task"], 10)
        report = {
            "verified": True,
            "model": "gpt-5.6-luna",
            "task_id": task_id,
            "native_skill_input": True,
            "native_terminal_input_resize_stop": True,
            "file_preview": True,
            "cache": True,
            "training_runs": 0,
        }
        (home / "workspace-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        if task_id:
            with contextlib.suppress(Exception):
                await session.update_access(task_id, AccessOptions(permissions=":read-only"))
            with contextlib.suppress(Exception):
                await session.rpc("thread/archive", {"threadId": task_id})
        await gateway.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument(
        "--terminal-permissions",
        default=":read-only",
        choices=[":read-only", ":workspace", ":danger-full-access"],
    )
    options = parser.parse_args()
    asyncio.run(run(options.home.resolve(), options.terminal_permissions))
