"""Verify native permissions and forks using tiny Luna conversations, without training."""

import argparse
import asyncio
import json
from pathlib import Path

from deepqueue.agent import AppServer
from deepqueue.config import initialize
from deepqueue.db import Database
from deepqueue.gateway import AccessOptions, CodexGateway, ForkThread, Message, NewThread
from deepqueue.models import CodexConnection, Server


async def run(home):
    if home.exists():
        raise ValueError("Choose a fresh test directory")
    initialize(home)
    db = Database(home)
    db.initialize()
    db.add_server(Server(name="local", codex=CodexConnection(enabled=True, cwd=str(home))))
    gateway = CodexGateway(home, db)
    session = await gateway.session("local")
    created = []
    try:
        await session.connect()
        source = (
            await session.new_thread(
                NewThread(
                    cwd=str(home),
                    model="gpt-5.6-luna",
                    permissions=":read-only",
                    approval_policy="never",
                )
            )
        )["thread"]["id"]
        created.append(source)
        changed = await session.update_access(source, AccessOptions(permissions=":workspace"))
        assert changed["thread"]["activePermissionProfile"]["id"] == ":workspace", changed
        async with asyncio.timeout(2):
            await session.update_access(source, AccessOptions(permissions=":workspace"))
        print("Native permissions updated before the first message", flush=True)
        events = asyncio.Queue(maxsize=256)
        session.subscribers.add(events)

        async def turn(thread_id, marker):
            await session.send_message(
                thread_id,
                Message(
                    text=f"Isolated DeepQueue protocol test. Reply exactly {marker}. "
                    "Do not use tools.",
                    model="gpt-5.6-luna",
                    effort="low",
                    request_id=marker,
                ),
            )
            async with asyncio.timeout(180):
                while True:
                    event = await events.get()
                    if (
                        event.get("method") == "turn/completed"
                        and event.get("params", {}).get("threadId") == thread_id
                    ):
                        result = event["params"]["turn"]
                        assert marker in AppServer.final_text(result), result
                        return result["id"]

        first_turn = await turn(source, "DQ_ACCESS_OK")
        await turn(source, "DQ_SOURCE_SECOND")
        fork = await session.fork_thread(
            source, ForkThread(last_turn_id=first_turn, request_id="native-fork")
        )
        branch = fork["thread"]["id"]
        created.append(branch)
        assert not fork.get("warnings"), fork.get("warnings")
        assert fork["model"] == "gpt-5.6-luna"
        assert fork["reasoningEffort"] == "low"
        assert fork["activePermissionProfile"]["id"] == ":workspace"
        assert fork["approvalPolicy"] == "never"
        await session.disconnect()
        await session.connect()
        branch_state = (await session.open_thread(branch, 40))["thread"]
        assert branch_state["forkedFromId"] == source
        assert len(branch_state["turns"]) == 1, branch_state
        await turn(branch, "DQ_BRANCH_ONLY")
        original = (await session.open_thread(source, 40))["thread"]
        assert len(original["turns"]) == 2
        assert "DQ_BRANCH_ONLY" not in json.dumps(original)
        report = {
            "verified": True,
            "model": "gpt-5.6-luna",
            "original": source,
            "branch": branch,
            "training_runs": 0,
            "source_unchanged": True,
            "permissions_persisted": True,
        }
        (home / "actions-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        # Only test conversations created in this isolated directory are archived.
        for thread_id in reversed(created):
            try:
                await session.rpc("thread/archive", {"threadId": thread_id})
            except Exception as exc:
                print(f"Test task retained for inspection: {thread_id}: {exc}", flush=True)
        await gateway.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    asyncio.run(run(parser.parse_args().home.resolve()))
