from __future__ import annotations

import io
import json
import shlex
import zipfile
from pathlib import Path

from .config import private_write
from .models import Server, service_url


def skill_files(url=None, server=None):
    source = Path(__file__).parent / "skill"
    files = {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    if bool(url) != bool(server):
        raise ValueError("A remote skill requires both --url and --target-server")
    if url:
        url = service_url(url)
        Server(name=server)
        command = shlex.join(["deepqueue", "--url", url, "--target-server", server])
        deployment = (
            "## This Deployment\n\n"
            f"Public queue: `{url}`. Bound execution server: `{server}`.\n"
            "This is a remote submission skill. The queue database and scheduler live on the "
            "public queue host. Training runs only on the bound execution server through SSH. "
            "Use the installed client credentials; never include tokens "
            "in a manifest or prompt.\n\n"
            f"Use `{command} ...` for preview, submission, monitoring, and improvement children. "
            "Do not initialize a local queue, substitute `local`, or change servers to get "
            "available GPUs. Insufficient capacity waits in this server's own queue. "
            "Bind `--from-agent` on the submitting machine using its actual CODEX_THREAD_ID.\n\n"
        )
        text = files["SKILL.md"].decode()
        files["SKILL.md"] = text.replace(
            "# DeepQueue\n\n", "# DeepQueue\n\n" + deployment, 1
        ).encode()
        files["deployment.json"] = json.dumps({"url": url, "server": server}, indent=2).encode()
    return files


def install_skill(destination, url=None, server=None):
    if destination.exists():
        raise ValueError(f"Skill destination already exists: {destination}")
    files = skill_files(url, server)
    destination.mkdir(parents=True, mode=0o700)
    for name, content in files.items():
        private_write(destination / name, content)
    return {"skill": "deepqueue", "path": str(destination), "url": url, "server": server}


def skill_archive(url, server):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in skill_files(url, server).items():
            archive.writestr("deepqueue/" + name, content)
    return output.getvalue()
