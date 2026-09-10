"""Small synthetic optimization experiment for end-to-end queue verification."""

import argparse
import json
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--learning-rate", type=float, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
started = time.monotonic()
value = 0.0
for _ in range(100):
    value -= args.learning_rate * 2 * (value - 3)
time.sleep(1)
result = {
    "learning_rate": args.learning_rate,
    "loss": (value - 3) ** 2,
    "value": value,
    "steps": 100,
    "elapsed_seconds": time.monotonic() - started,
    "dataset": "synthetic_scalar_objective",
    "gpu_count": 0,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result), flush=True)
