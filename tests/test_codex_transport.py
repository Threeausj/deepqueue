"""Run npm-style launchers with a sibling interpreter and a minimal SSH PATH."""

import io
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest
from test_ssh import Auth, loopback_ssh

from deepqueue.codex_transport import SSHCodexSocket, start_daemon
from deepqueue.config import Secrets
from deepqueue.models import CodexConnection, Server
from deepqueue.transport import read_channel, trust_host


@pytest.fixture
def node_install(tmp_path):
    # Also exercise spaces, shell quoting and npm's symlink into lib/node_modules.
    install = tmp_path / "nvm 'node' $(touch INJECTED)" / "v24.14.1"
    bin_dir = install / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "node").symlink_to(sys.executable)
    script = install / "lib/node_modules/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    calls = tmp_path / "calls.jsonl"
    # Python stands in for Node; /usr/bin/env still resolves the real shebang.
    script.write_text(
        "#!/usr/bin/env node\n"
        "import json, os, sys\n"
        "record = json.dumps({'args': sys.argv[1:], 'path': os.environ['PATH']})\n"
        f"with open({str(calls)!r}, 'a') as log: log.write(record + '\\n')\n"
        "print(record)\n"
    )
    script.chmod(0o755)
    executable = bin_dir / "codex"
    executable.symlink_to(script)
    minimal_path = tmp_path / "system-bin"
    minimal_path.mkdir()
    return executable, minimal_path, calls


@pytest.mark.parametrize("shell_name", ["sh", "zsh"])
def test_actual_ssh_start_and_proxy_find_sibling_node(
    db, tmp_path, monkeypatch, node_install, shell_name
):
    shell = shutil.which(shell_name)
    if not shell:
        pytest.skip(f"{shell_name} is not installed")
    executable, minimal_path, calls = node_install
    env = {**os.environ, "PATH": str(minimal_path)}
    before = subprocess.run([str(executable), "--version"], env=env, capture_output=True)
    assert before.returncode == 127 and b"node" in before.stderr

    def execute(self, channel, command):
        def run():
            result = subprocess.run(
                [shell, "-c", command.decode()],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                timeout=10,
            )
            if result.stdout:
                channel.sendall(result.stdout)
            if result.stderr:
                channel.sendall_stderr(result.stderr)
            channel.send_exit_status(result.returncode)
            channel.close()

        threading.Thread(target=run, daemon=True).start()
        return True

    monkeypatch.setattr(Auth, "check_channel_exec_request", execute)
    with loopback_ssh(tmp_path) as ssh:
        trust_host(db.home, "127.0.0.1", ssh["port"], ssh["fingerprint"])
        server = Server(
            name="nvm",
            kind="ssh",
            host="127.0.0.1",
            port=ssh["port"],
            username="fixture",
            password_ref=Secrets(db.home).put("fixture-password"),
            codex=CodexConnection(enabled=True, executable=str(executable)),
        )
        start_daemon(db.home, server)
        socket_path = str(tmp_path / "socket 'quoted'")
        server.codex.socket = socket_path
        bridge = SSHCodexSocket(db.home, server, io.BytesIO())
        context, channel = bridge._open()
        try:
            output, error, code = read_channel(channel, timeout=10)
            assert code == 0, error
            assert json.loads(output)["args"] == ["app-server", "proxy", "--sock", socket_path]
        finally:
            channel.close()
            context.__exit__(None, None, None)

    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [row["args"] for row in records] == [
        ["app-server", "daemon", "start"],
        ["app-server", "proxy", "--sock", socket_path],
    ]
    assert all(
        row["path"].split(":") == [str(executable.parent), str(minimal_path)] for row in records
    )
    assert not (tmp_path / "INJECTED").exists()


def test_local_start_finds_sibling_node_without_changing_service_environment(
    tmp_path, monkeypatch, node_install
):
    executable, minimal_path, calls = node_install
    monkeypatch.setenv("PATH", str(minimal_path))
    server = Server(name="local", codex=CodexConnection(executable=str(executable)))
    start_daemon(tmp_path, server)
    record = json.loads(calls.read_text())
    assert record["args"] == ["app-server", "daemon", "start"]
    assert record["path"].split(":") == [str(executable.parent), str(minimal_path)]
    assert os.environ["PATH"] == str(minimal_path)
