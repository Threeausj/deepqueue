"""Long process history and native streaming events; no commands or models run."""

from workspace_fixture import add_workspace


def command(item_id, index=0, active=False):
    return {
        "id": item_id,
        "type": "commandExecution",
        "command": f"python train.py --trial {index} --batch-size 8 --config configs/model.json",
        "status": "inProgress" if active else "completed",
        "exitCode": None if active else 0,
        "aggregatedOutput": "UI fixture output\n",
    }


async def activity(fixture, root, action):
    tid = "activity-task"
    if action == "setup":
        add_workspace(fixture, root)
        thread = fixture.add_thread(tid, "整段折叠验证", cwd=str(root))
        items = [
            {
                "id": "activity-user",
                "type": "userMessage",
                "content": [{"type": "text", "text": "比较实验结果，保留精度变化。"}],
            },
            {
                "id": "activity-progress",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "先检查提交的代码和运行参数。",
            },
        ]
        for index in range(6):
            items.extend(
                [
                    {
                        "id": f"reason-{index}",
                        "type": "reasoning",
                        "summary": [f"协议测试思考摘要 {index}"],
                    },
                    command(f"command-{index}", index),
                ]
            )
        items[5].update(
            status="failed", exitCode=1, aggregatedOutput="UI fixture: initial command failed\n"
        )
        items.extend(
            [
                {
                    "id": "activity-file",
                    "type": "fileChange",
                    "status": "completed",
                    "changes": [
                        {
                            "path": str(root / "notes/train.py"),
                            "kind": {"type": "update", "move_path": None},
                            "diff": "+accuracy = 0.93\n",
                        }
                    ],
                },
                {
                    "id": "activity-final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "最终结论：验证精度由 0.91 提高到 0.93。",
                },
            ]
        )
        thread["turns"] = [
            {"id": "activity-history", "status": "completed", "items": items},
            {
                "id": "legacy-history",
                "status": "completed",
                "items": [
                    {
                        "id": "legacy-user",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "读取旧版记录"}],
                    },
                    {"id": "legacy-reason", "type": "reasoning", "summary": ["旧版协议摘要"]},
                    command("legacy-command"),
                    {
                        "id": "legacy-final",
                        "type": "agentMessage",
                        "text": "旧版模型回复保持可见。",
                    },
                ],
            },
        ]
        return {"thread_id": tid}
    thread = fixture.threads[tid]
    if action == "start":
        turn = {
            "id": "activity-live",
            "status": "inProgress",
            "items": [
                {
                    "id": "live-user",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "继续检查运行结果"}],
                },
                {"id": "live-reason", "type": "reasoning", "summary": []},
                command("live-command", active=True),
            ],
        }
        thread["turns"].append(turn)
        thread["status"] = {"type": "active"}
        await fixture.emit("turn/started", {"threadId": tid, "turn": turn})
    else:
        turn = thread["turns"][-1]
        params = {"threadId": tid, "turnId": turn["id"]}
        if action == "delta":
            turn["items"][1]["summary"] = ["实时摘要更新（协议测试）"]
            await fixture.emit(
                "item/reasoning/summaryTextDelta",
                {**params, "itemId": "live-reason", "delta": "实时摘要更新（协议测试）"},
            )
            turn["items"][2]["aggregatedOutput"] += "streamed accuracy = 0.94\n"
            await fixture.emit(
                "item/commandExecution/outputDelta",
                {**params, "itemId": "live-command", "delta": "streamed accuracy = 0.94\n"},
            )
            item = command("live-command-2", index=2, active=True)
            turn["items"].append(item)
            await fixture.emit("item/started", {**params, "item": item})
        elif action == "finish":
            for item in turn["items"]:
                if item["type"] == "commandExecution":
                    item.update(status="completed", exitCode=0)
                    await fixture.emit("item/completed", {**params, "item": item})
            item = {"id": "live-final", "type": "agentMessage", "phase": "final_answer", "text": ""}
            turn["items"].append(item)
            await fixture.emit("item/started", {**params, "item": item})
            item["text"] = "本轮最终回复始终可见。"
            await fixture.emit(
                "item/agentMessage/delta", {**params, "itemId": item["id"], "delta": item["text"]}
            )
            await fixture.emit("item/completed", {**params, "item": item})
            turn["status"] = "completed"
            thread["status"] = {"type": "idle"}
            await fixture.emit("turn/completed", {"threadId": tid, "turn": turn})
        else:
            raise ValueError("Unknown activity fixture action")
    return {"ok": True}
