"""Exercise an image using disposable Docker volumes; no models or training."""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Docker command failed")
    return result.stdout.strip()


def wait_for(action, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = action()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Docker smoke test readiness timed out")


def run(image, build):
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required; run this script on the deployment host")
    docker("info", "--format", "{{.ServerVersion}}")
    if build:
        subprocess.run(
            ["docker", "build", "--tag", image, "."],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
        )
    prefix = "deepqueue-smoke-" + uuid.uuid4().hex[:12]
    volumes = [prefix + "-queue", prefix + "-codex"]
    mounts = [
        "--mount",
        f"type=volume,source={volumes[0]},target=/var/lib/deepqueue",
        "--mount",
        f"type=volume,source={volumes[1]},target=/home/deepqueue/.codex",
        "--env",
        "DEEPQUEUE_PUBLIC_URL=https://queue.example.test",
    ]
    try:
        for volume in volumes:
            docker("volume", "create", volume)
        bootstrap = json.loads(docker("run", "--rm", *mounts, image, "setup"))
        token = bootstrap["admin"]["token"]  # Keep the test credential out of argv and logs.
        docker(
            "run",
            "--detach",
            "--init",
            "--name",
            prefix,
            "--publish",
            "127.0.0.1::8765",
            *mounts,
            image,
        )

        def healthy():
            state = json.loads(docker("inspect", "--format", "{{json .State}}", prefix))
            if not state["Running"]:
                raise RuntimeError("The test container exited during startup")
            return state.get("Health", {}).get("Status") == "healthy"

        wait_for(healthy)
        url = "http://" + docker("port", prefix, "8765/tcp").splitlines()[0]

        def api(path, data=None, authenticated=True):
            headers = {"X-DeepQueue": "1"}
            if authenticated:
                headers["Authorization"] = "Bearer " + token
            if data is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                url + path,
                headers=headers,
                data=json.dumps(data).encode() if data is not None else None,
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)

        try:
            api("/api/state", authenticated=False)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Queue data must require authentication")
        state = api("/api/state")
        assert state["jobs"] == [] and not state["daemon"]["running"]
        assert not state["servers"][0]["config"]["enabled"]
        assert "codex" in docker("exec", prefix, "codex", "--version").lower()
        for action in ("start", "stop", "start"):
            api("/api/daemon", {"action": action})
            assert api("/api/state")["daemon"]["running"] == (action == "start")
        docker("stop", "--timeout", "60", prefix)
        assert not json.loads(
            docker("run", "--rm", *mounts, image, "deepqueue", "daemon", "status")
        )["running"]
        docker("start", prefix)
        wait_for(healthy)
        url = "http://" + docker("port", prefix, "8765/tcp").splitlines()[0]
        assert not api("/api/state")["daemon"]["running"]
        docker("stop", "--timeout", "60", prefix)
        repeated = json.loads(docker("run", "--rm", *mounts, image, "setup"))
        assert not repeated["admin_created"] and "admin" not in repeated
        print(
            json.dumps(
                {
                    "verified": True,
                    "image": image,
                    "training_runs": 0,
                    "model_calls": 0,
                    "auth_and_persistence": True,
                    "dashboard_scheduler_controls": True,
                    "graceful_shutdown": True,
                }
            )
        )
    except Exception:
        try:
            print(docker("logs", "--tail", "60", prefix), file=sys.stderr)
        except RuntimeError:
            pass
        raise
    finally:
        # Only names allocated by this invocation are removed, never production volumes.
        for args in [("rm", "--force", prefix), *[("volume", "rm", v) for v in volumes]]:
            try:
                docker(*args)
            except RuntimeError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="deepqueue:local")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    try:
        run(args.image, args.build)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
