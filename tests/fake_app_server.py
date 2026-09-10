"""Protocol fixture, not an inference backend."""

import json
import sys

from deepqueue.models import Estimate, Resources


def send(data):
    print(json.dumps(data), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fixture"}})
    elif method == "thread/start":
        assert message["params"]["sandbox"] == "read-only"
        assert message["params"]["model"] == "gpt-5.6-luna"
        send({"id": message["id"], "result": {"thread": {"id": "fixture-thread"}}})
    elif method == "turn/start":
        send(
            {
                "id": message["id"],
                "result": {"turn": {"id": "fixture-turn", "status": "inProgress"}},
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "another-thread",
                    "turn": {"id": "other-turn", "status": "failed"},
                },
            }
        )
        answer = Estimate(
            resources=Resources(),
            confidence=0.95,
            rationale="CPU fixture",
            evidence=["true command"],
            warnings=[],
            needs_review=False,
        )
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "fixture-thread",
                    "turnId": "fixture-turn",
                    "item": {
                        "type": "agentMessage",
                        "id": "answer",
                        "phase": "final_answer",
                        "text": answer.model_dump_json(),
                    },
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "fixture-thread",
                    "turn": {"id": "fixture-turn", "status": "completed"},
                },
            }
        )
