import json
import os
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from test_ssh import Auth, loopback_ssh

from deepqueue import codex_discovery as discovery
from deepqueue.cli import execute, parser
from deepqueue.codex_transport import discover_codex
from deepqueue.config import Secrets
from deepqueue.models import CodexConnection, Server
from deepqueue.transport import trust_host
from deepqueue.web import create_app


@pytest.fixture
def account(tmp_path, monkeypatch):
    root = tmp_path / "account"
    root.mkdir()
    system = root / "system-bin"
    system.mkdir()
    monkeypatch.setenv("PATH", str(system))
    monkeypatch.setattr(discovery.Path, "home", lambda: root)
    monkeypatch.setattr(discovery, "SYSTEM_BINS", ())
    monkeypatch.setattr(discovery.platform, "machine", lambda: "x86_64")
    for name in (
        "NVM_DIR",
        "NVM_BIN",
        "CODEX_INSTALL_DIR",
        "npm_config_prefix",
        "NPM_CONFIG_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    return root, system


def npm_install(prefix, *, node=True, link=True):
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    package = prefix / "lib/node_modules/@openai/codex"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "@openai/codex", "version": "0.153.4"})
    )
    script = package / "bin/codex.js"
    script.write_text(
        "#!/usr/bin/env node\nraise RuntimeError('Discovery must not execute Codex')\n"
    )
    script.chmod(0o755)
    if link:
        (bin_dir / "codex").symlink_to(script)
    if node:
        (bin_dir / "node").symlink_to(sys.executable)
    native = (
        package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
    )
    native.parent.mkdir(parents=True)
    native.write_text("#!/bin/sh\nexit 0\n")
    native.chmod(0o755)
    return bin_dir / "codex", script, native


def test_nvm_versions_missing_path_and_matching_node(account):
    root, _ = account
    npm_install(root / ".nvm/versions/node/v9.0.0")
    launcher, script, native = npm_install(root / ".nvm/versions/node/v24.14.1")
    report = discovery.discover()
    assert report["recommended"] == str(launcher)
    found = {item["path"]: item for item in report["candidates"]}
    assert found[str(launcher)]["node"] == str(launcher.parent / "node")
    assert found[str(script)]["node"] == str(launcher.parent / "node")
    assert found[str(native)]["kind"] == "npm-native"
    assert found[str(native)]["ready"] and found[str(native)]["node"] is None
    assert found[str(launcher)]["version"] == "0.153.4"
    assert discovery.discover(str(script))["recommended"] == str(script)


def test_npm_custom_prefix_and_missing_bin_link(account):
    root, system = account
    launcher, script, native = npm_install(root / "custom 'prefix' $(touch INJECTED)", link=False)
    prefix = launcher.parent.parent
    npm = system / "npm"
    calls = root / "npm-calls.json"
    npm.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"print({str(prefix)!r})\n"
    )
    npm.chmod(0o755)
    report = discovery.discover()
    assert report["recommended"] == str(script)
    assert str(native) in [item["path"] for item in report["candidates"]]
    assert json.loads(calls.read_text()) == ["prefix", "--global"]
    assert not (root / "INJECTED").exists()


def test_missing_node_uses_native_binary_for_target_architecture(account):
    root, _ = account
    launcher, _, native = npm_install(root / ".nvm/versions/node/v24.14.1", node=False)
    wrong_arch = native.parents[2] / "aarch64-unknown-linux-musl/bin/codex"
    wrong_arch.parent.mkdir(parents=True)
    wrong_arch.write_text("#!/bin/sh\nexit 0\n")
    wrong_arch.chmod(0o755)
    report = discovery.discover()
    assert report["recommended"] == str(native)
    found = {item["path"]: item for item in report["candidates"]}
    assert not found[str(launcher)]["ready"] and "Node" in found[str(launcher)]["problem"]
    assert str(wrong_arch) not in found
    native.unlink()
    report = discovery.discover()
    assert report["recommended"] is None and all(not c["ready"] for c in report["candidates"])


def test_missing_install_is_explicit_and_does_not_scan_projects(account, monkeypatch):
    root, system = account
    assert discovery.discover()["candidates"] == []
    launcher, _, _ = npm_install(root / "project")
    monkeypatch.chdir(launcher.parent)
    monkeypatch.setenv("PATH", str(system) + ":.")
    # An explicit path can be inspected; a relative PATH entry is not searched implicitly.
    assert discovery.discover()["candidates"] == []


def test_local_cli_and_http_detection_preserve_configuration(db, account):
    root, _ = account
    launcher, _, _ = npm_install(root / ".nvm/versions/node/v24.14.1")
    before = db.server("local")["config"]["codex"]
    args = parser().parse_args(["--home", str(db.home), "codex", "detect", "local"])
    assert execute(args)["recommended"] == str(launcher)
    with TestClient(create_app(db.home), base_url="http://localhost") as web:
        report = web.get("/api/servers/local/codex/executables").json()
        assert report["server"] == "local" and report["recommended"] == str(launcher)
        assert web.get("/api/servers/local/codex").json()["state"] == "disconnected"
    assert db.server("local")["config"]["codex"] == before


def test_detection_runs_on_ssh_server_with_quoted_paths(db, tmp_path, monkeypatch):
    prefix = tmp_path / "remote 'node' $(touch INJECTED)"
    launcher, script, native = npm_install(prefix)
    env = {**os.environ, "PATH": str(prefix / "bin")}

    def execute_command(self, channel, command):
        def run():
            result = subprocess.run(
                ["/bin/sh", "-c", command.decode()],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                timeout=18,
            )
            if result.stdout:
                channel.sendall(result.stdout)
            if result.stderr:
                channel.sendall_stderr(result.stderr)
            channel.send_exit_status(result.returncode)
            channel.close()

        threading.Thread(target=run, daemon=True).start()
        return True

    monkeypatch.setattr(Auth, "check_channel_exec_request", execute_command)
    with loopback_ssh(tmp_path) as ssh:
        trust_host(db.home, "127.0.0.1", ssh["port"], ssh["fingerprint"])
        server = Server(
            name="npm-ssh",
            kind="ssh",
            host="127.0.0.1",
            port=ssh["port"],
            username="fixture",
            python=sys.executable,
            password_ref=Secrets(db.home).put("fixture-password"),
            codex=CodexConnection(executable=str(launcher)),
        )
        report = discover_codex(db.home, server)
        assert report["server"] == "npm-ssh" and report["recommended"] == str(launcher)
        paths = {row["path"] for row in report["candidates"]}
        assert str(script) in paths and str(native) in paths
    assert not (tmp_path / "INJECTED").exists()


def test_missing_node_prefers_same_npm_install_over_another_version(account):
    root, _ = account
    launcher, _, native = npm_install(root / ".nvm/versions/node/v24.14.1", node=False)
    npm_install(root / ".nvm/versions/node/v20.0.0")
    assert discovery.discover(str(launcher))["recommended"] == str(native)
    report = discovery.discover(str(native))
    assert report["recommended"] == str(native)
    assert report["candidates"][0]["kind"] == "npm-native"
