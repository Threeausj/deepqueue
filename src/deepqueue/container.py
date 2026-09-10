"""Docker entrypoint: one web process and its CLI-controlled scheduler per queue."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from .access import Access
from .cli import daemon_state, execute, parser
from .config import load_settings, state_home, update_settings
from .db import Database
from .models import service_url


@contextlib.contextmanager
def ownership(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / "container.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("This queue already has a running container; stop it first") from exc
        yield


def public_url():
    value = os.environ.get("DEEPQUEUE_PUBLIC_URL", "").strip()
    if not value:
        raise ValueError("Set DEEPQUEUE_PUBLIC_URL to this deployment's HTTPS origin")
    return service_url(value)


def local_command(home, *args):
    return execute(parser().parse_args(["--home", str(home), *args]))


def setup(home):
    """Explicit bootstrap; credentials are emitted only by this operator command."""
    url = public_url()
    with ownership(home):
        fresh = not (home / "config.json").exists()
        local_command(home, "init")
        if fresh:
            # In Docker, local means the coordinator container, not its GPU host.
            Database(home).enable_server("local", False)
        access = Access(home)
        admin = None if access.has_admin() else access.create("docker-bootstrap-admin")
        update_settings(home, {"public_url": url})
        result = {"home": str(home), "public_url": url, "admin_created": admin is not None}
        if admin:
            result["admin"] = admin
        return result


def start_on_boot():
    value = os.environ.get("DEEPQUEUE_START_SCHEDULER", "false").lower()
    if value not in ("true", "false", "1", "0"):
        raise ValueError("DEEPQUEUE_START_SCHEDULER must be true or false")
    return value in ("true", "1")


def port():
    value = int(os.environ.get("DEEPQUEUE_WEB_PORT", "8765"))
    if not 1 <= value <= 65535:
        raise ValueError("DEEPQUEUE_WEB_PORT must be between 1 and 65535")
    return value


def healthcheck():
    # Auth status is public but discloses no queue data; no admin token in health logs.
    with urllib.request.urlopen(f"http://127.0.0.1:{port()}/api/auth", timeout=3) as response:
        if response.status != 200 or json.load(response).get("required") is not True:
            raise ValueError("The authenticated dashboard is not ready")


def stop_children(home, web):
    # Stop accepting new start requests, then use the same identity-checked shutdown
    # as the CLI. This also finds a scheduler started later through the dashboard.
    if web:
        if web.poll() is None:
            web.terminate()
        try:
            web.wait(timeout=8)
        except subprocess.TimeoutExpired:
            web.kill()
            web.wait(timeout=3)
    # Drain the web process before inspecting the lock: an in-flight start request
    # may still be creating a scheduler during web shutdown.
    local_command(home, "daemon", "stop")
    deadline = time.monotonic() + 20
    while daemon_state(home)["running"] and time.monotonic() < deadline:
        time.sleep(0.1)
    if daemon_state(home)["running"]:
        raise RuntimeError("Scheduler shutdown timed out; inspect scheduler.log")


def serve(home):
    url, auto_start, web_port = public_url(), start_on_boot(), port()
    with ownership(home):
        if not (home / "config.json").exists() or not Access(home).has_admin():
            raise ValueError("Run 'docker compose run --rm deepqueue setup' before starting")
        if daemon_state(home)["running"]:
            raise ValueError("Stop the existing scheduler before starting this container")
        # Startup may migrate the database; the sole-owner check happens first.
        local_command(home, "init")
        if load_settings(home).public_url != url:
            update_settings(home, {"public_url": url})
        stopping = threading.Event()
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stopping.set())
        web = None
        try:
            if auto_start and not stopping.is_set():
                local_command(home, "daemon", "start")
            if stopping.is_set():
                return 0
            web = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "deepqueue",
                    "--home",
                    str(home),
                    "web",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(web_port),
                ]
            )
            while web.poll() is None and not stopping.wait(0.2):
                pass
            return 0 if stopping.is_set() else (web.returncode or 1)
        finally:
            try:
                stop_children(home, web)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main():
    os.umask(0o077)
    args = sys.argv[1:] or ["serve"]
    try:
        if args == ["setup"]:
            print(json.dumps(setup(state_home()), ensure_ascii=False, indent=2))
        elif args == ["serve"]:
            return serve(state_home())
        elif args == ["healthcheck"]:
            healthcheck()
        else:
            # Operator commands do not bootstrap, start the scheduler or run a web server.
            os.execvp(args[0], args)
    except Exception as exc:
        print(f"DeepQueue container: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
