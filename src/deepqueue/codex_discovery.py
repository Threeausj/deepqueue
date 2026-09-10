"""Read-only Codex installation discovery, also sent over SSH as a stdlib script."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM_BINS = ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin")


def command_directories(executable):
    """Preserve npm's bin symlink and find Node for a direct npm JS entrypoint."""
    if not executable.startswith("/"):
        return []
    directories = [str(Path(executable).parent)]
    if "/lib/node_modules/" in executable:
        directories.append(executable.split("/lib/node_modules/", 1)[0] + "/bin")
    return list(dict.fromkeys(directories))


def command_environment(executable):
    env = os.environ.copy()
    directories = command_directories(executable)
    if directories:
        env["PATH"] = os.pathsep.join([*directories, env.get("PATH") or os.defpath])
    return env


def runnable(path):
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def package_info(path):
    try:
        for parent in list(path.resolve().parents)[:9]:
            metadata = parent / "package.json"
            if metadata.is_file() and metadata.stat().st_size <= 65536:
                data = json.loads(metadata.read_text())
                if isinstance(data, dict) and data.get("name") == "@openai/codex":
                    version = data.get("version")
                    return parent, version if isinstance(version, str) else None
    except (OSError, ValueError, RuntimeError):
        pass
    return None, None


def native_candidates(package):
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        platform.machine().lower()
    )
    target = {"linux": "unknown-linux-musl", "darwin": "apple-darwin"}.get(sys.platform)
    if not arch or not target:
        return []
    triple = f"{arch}-{target}"
    tag = f"codex-{sys.platform}-{'x64' if arch == 'x86_64' else 'arm64'}"
    roots = [package, package / "node_modules/@openai" / tag, package.parent / tag]
    return [
        root / "vendor" / triple / directory / "codex"
        for root in roots
        for directory in ("bin", "codex")
    ]


def discover(configured="codex"):
    candidates, seen, packages, prefixes, npm_paths = [], set(), set(), set(), []
    warnings = []

    def add(path, source, native=False):
        path = Path(path).expanduser().absolute()
        if str(path) in seen or len(candidates) >= 64 or not runnable(path):
            return
        seen.add(str(path))
        package, version = package_info(path)
        try:
            with path.open("rb") as handle:
                shebang = handle.read(160).split(b"\n", 1)[0]
        except OSError:
            return
        needs_node = shebang.startswith(b"#!") and b"node" in shebang.split()
        node = (
            shutil.which("node", path=command_environment(str(path))["PATH"])
            if needs_node
            else None
        )
        native = native or bool(package and not needs_node and "vendor" in path.resolve().parts)
        candidate = {
            "path": str(path),
            "source": source,
            "kind": "npm-native" if native else "npm" if package or needs_node else "binary",
            "version": version,
            "node": node,
            "ready": not needs_node or bool(node),
            "problem": "此 npm 启动入口需要 Node，但未在对应安装目录或 PATH 中找到。"
            if needs_node and not node
            else None,
        }
        candidates.append(candidate)
        if package:
            packages.add(package)

    def add_bin(directory, source):
        directory = Path(directory).expanduser().absolute()
        add(directory / "codex", source)
        if (
            source != "configured"
            and runnable(directory / "npm")
            and str(directory / "npm") not in npm_paths
        ):
            npm_paths.append(str(directory / "npm"))
        if directory.name == "bin":
            prefixes.add(directory.parent)

    search_path = os.pathsep.join(
        directory for directory in os.get_exec_path() if os.path.isabs(directory)
    )
    configured_path = Path(configured).expanduser()
    resolved = (
        str(configured_path.absolute())
        if "/" in configured
        else shutil.which(configured, path=search_path)
    )
    if resolved:
        add(resolved, "configured")
        add_bin(Path(resolved).parent, "configured")
    for directory in os.get_exec_path():
        # Avoid implicitly discovering executables inside an arbitrary project directory.
        if os.path.isabs(directory):
            add_bin(directory, "path")
    for directory in (
        *SYSTEM_BINS,
        Path.home() / ".local/bin",
        Path.home() / ".npm-global/bin",
        Path.home() / ".npm/bin",
    ):
        add_bin(directory, "common")
    for name in ("NVM_BIN", "CODEX_INSTALL_DIR"):
        if value := os.environ.get(name):
            add_bin(value, "nvm" if name == "NVM_BIN" else "common")
    for name in ("npm_config_prefix", "NPM_CONFIG_PREFIX"):
        if value := os.environ.get(name):
            add_bin(Path(value) / "bin", "npm")
    nvm = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    versions = sorted(
        nvm.glob("versions/node/*/bin"),
        key=lambda p: tuple(int(x) for x in re.findall(r"\d+", p.parent.name)),
        reverse=True,
    )
    for directory in versions[:32]:
        add_bin(directory, "nvm")

    # Read npm's configured global prefix. Never install packages or run project scripts.
    for npm in npm_paths[:3]:
        env = command_environment(npm)
        env["NO_UPDATE_NOTIFIER"] = "1"
        try:
            result = subprocess.run(
                [npm, "prefix", "--global"],
                cwd=Path.home(),
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
            )
            prefix = result.stdout.strip()
            if (
                result.returncode == 0
                and prefix.startswith("/")
                and "\n" not in prefix
                and len(prefix) <= 4096
            ):
                add_bin(Path(prefix) / "bin", "npm")
            else:
                warnings.append(f"未能读取 {npm} 的全局安装目录。")
        except (OSError, subprocess.TimeoutExpired):
            warnings.append(f"查询 {npm} 的全局安装目录失败或超时。")
    for prefix in sorted(prefixes):
        package = prefix / "lib/node_modules/@openai/codex"
        if package.is_dir():
            packages.add(package)
    for package in sorted(packages):
        add(package / "bin/codex.js", "npm")
        for binary in native_candidates(package):
            add(binary, "npm", native=True)

    # An explicitly found launcher wins. Missing Node can use the same package's native binary.
    ready = [item for item in candidates if item["ready"]]
    recommended = ready[0]["path"] if ready else None
    if candidates and not candidates[0]["ready"]:
        package, _ = package_info(Path(candidates[0]["path"]))
        if package:
            native_paths = {str(path.absolute()) for path in native_candidates(package)}
            preferred = next((item for item in ready if item["path"] in native_paths), None)
            if preferred:
                recommended = preferred["path"]
    return {"candidates": candidates, "recommended": recommended, "warnings": warnings}


if __name__ == "__main__":
    print(json.dumps(discover(sys.argv[1] if len(sys.argv) > 1 else "codex"), ensure_ascii=False))
