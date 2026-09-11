"""Synthetic context counts and native compaction notifications; no model calls."""


def usage(last=96000, window=128000):
    def counts(total):
        return {
            "totalTokens": total,
            "inputTokens": total - 1000,
            "cachedInputTokens": 4000,
            "outputTokens": 1000,
            "reasoningOutputTokens": 500,
        }

    return {"last": counts(last), "total": counts(540000), "modelContextWindow": window}


async def context_fixture(fixture, root, action):
    tid = "context-task"
    if action == "setup":
        thread = fixture.add_thread(tid, "归档与上下文验证", cwd=str(root))
        archive = "DeepQueue 实验归档（界面测试数据）\n" + "\n".join(
            f"第 {i + 1} 轮：验证精度 0.93，loss 0.12。请比较指标变化并给出下一轮建议。"
            for i in range(45)
        )
        inputs = [
            [{"type": "text", "text": "比较两次实验的验证精度。"}],
            [
                {"type": "text", "text": archive},
                {"type": "skill", "name": "deepqueue"},
                {"type": "image", "url": "fixture-image"},
                {"type": "text", "text": "归档结束标记：原始输入应完整保留。"},
            ],
            [{"type": "text", "text": "窗口宽度适配。" * 30}],
            [{"type": "text", "text": "/data/" + "a" * 900 + "/metrics.json"}],
        ]
        thread["turns"] = [
            {
                "id": f"context-history-{i}",
                "status": "completed",
                "items": [
                    {"id": f"context-user-{i}", "type": "userMessage", "content": content},
                    {
                        "id": f"context-answer-{i}",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "归档结论：验证精度较上次提高 2 个百分点。",
                    },
                ],
            }
            for i, content in enumerate(inputs)
        ]
        return {"thread_id": tid, "archive": archive}
    thread = fixture.threads[tid]
    if action in ("usage", "unknown-window"):
        await fixture.emit(
            "thread/tokenUsage/updated",
            {
                "threadId": tid,
                "turnId": thread["turns"][-1]["id"],
                "tokenUsage": usage(window=None if action == "unknown-window" else 128000),
            },
        )
    elif action == "start":
        turn = {"id": f"compact-{len(thread['turns'])}", "items": [], "status": "inProgress"}
        thread["turns"].append(turn)
        thread["status"] = {"type": "active"}
        await fixture.emit("turn/started", {"threadId": tid, "turn": turn})
        item = {"id": turn["id"] + "-item", "type": "contextCompaction"}
        turn["items"].append(item)
        await fixture.emit("item/started", {"threadId": tid, "turnId": turn["id"], "item": item})
    elif action in ("finish", "fail", "interrupt"):
        turn = thread["turns"][-1]
        params = {"threadId": tid, "turnId": turn["id"]}
        if action == "finish":
            await fixture.emit("thread/tokenUsage/updated", {**params, "tokenUsage": usage(12000)})
            await fixture.emit("item/completed", {**params, "item": turn["items"][0]})
        turn["status"] = {"finish": "completed", "fail": "failed", "interrupt": "interrupted"}[
            action
        ]
        thread["status"] = {"type": "idle"}
        await fixture.emit("turn/completed", {"threadId": tid, "turn": turn})
    else:
        raise ValueError("Unknown context fixture action")
    return {"ok": True}
