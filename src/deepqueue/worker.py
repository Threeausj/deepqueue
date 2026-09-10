"""Standalone Linux worker. Deployed over SFTP; only Python's standard library is required."""

from __future__ import annotations

import csv
import ctypes
import fcntl
import hashlib
import io
import json
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(value, f, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {
            "pid": pid,
            "start": stat[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, IndexError):
        return None


def alive(record):
    return bool(record and identity(record["pid"]) == record)


def group_members(pgid):
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(stat[2]) == pgid and stat[0] != "Z":
                rss = int(stat[21]) * os.sysconf("SC_PAGE_SIZE")
                members.append((int(entry.name), rss))
        except (OSError, ValueError, IndexError):
            pass
    return members


def subreaper(enabled=None):
    libc = ctypes.CDLL(None, use_errno=True)
    get_child_subreaper, set_child_subreaper = 37, 36
    previous = ctypes.c_int()
    if libc.prctl(get_child_subreaper, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot read child subreaper state")
    if enabled is not None and libc.prctl(set_child_subreaper, int(enabled), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot set child subreaper state")
    return previous.value


class ProcessTree:
    def __init__(self):
        self.owner = os.getpid()
        self.known = {}
        self.baseline = {
            pid: row["start"] for pid, row in self.snapshot().items() if row["parent"] == self.owner
        }

    @staticmethod
    def snapshot():
        rows = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                rows[int(entry.name)] = {
                    "parent": int(stat[1]),
                    "start": stat[19],
                    "state": stat[0],
                    "rss": int(stat[21]) * os.sysconf("SC_PAGE_SIZE"),
                }
            except (OSError, ValueError, IndexError):
                pass
        return rows

    def members(self):
        rows = self.snapshot()
        owned = {
            pid
            for pid, row in rows.items()
            if self.known.get(pid) == row["start"]
            or (row["parent"] == self.owner and self.baseline.get(pid) != row["start"])
        }
        # Elastic launchers use new sessions. Follow ancestry and adopted orphans.
        children = {}
        for pid, row in rows.items():
            children.setdefault(row["parent"], []).append(pid)
        pending = list(owned)
        while pending:
            for pid in children.get(pending.pop(), []):
                if pid not in owned:
                    owned.add(pid)
                    pending.append(pid)
        self.known = {pid: rows[pid]["start"] for pid in owned}
        return [(pid, rows[pid]["rss"]) for pid in owned if rows[pid]["state"] != "Z"]

    def signal(self, signum):
        for pid, _ in self.members():
            current = identity(pid)
            if current and current["start"] == self.known.get(pid):
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass

    def reap(self):
        rows = self.snapshot()
        for pid in self.known:
            if rows.get(pid, {}).get("start") != self.known[pid]:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def nvidia_query(query):
    result = subprocess.run(
        ["nvidia-smi", query, "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return [[item.strip() for item in row] for row in csv.reader(io.StringIO(result.stdout)) if row]


def gpu_processes():
    return [
        {"uuid": row[0], "pid": int(row[1]), "memory_mib": int(row[2])}
        for row in nvidia_query("--query-compute-apps=gpu_uuid,pid,used_gpu_memory")
    ]


def gpu_binding_error(allocated, usage, observed, active_seconds):
    unexpected = set(usage) - set(allocated)
    if unexpected:
        return "GPU allocation mismatch: unallocated GPUs " + ", ".join(sorted(unexpected))
    missing = set(allocated) - observed
    if active_seconds >= 120 and missing:
        return "GPU allocation mismatch: unused allocated GPUs " + ", ".join(sorted(missing))
    return None


def cpu_times():
    values = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def probe():
    before = cpu_times()
    time.sleep(0.15)
    after = cpu_times()
    idle_ratio = (after[1] - before[1]) / max(1, after[0] - before[0])
    cpus = len(os.sched_getaffinity(0))
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.split()[0]) // 1024
    result = {
        "observed_at": time.time(),
        "cpu_count": cpus,
        "cpu_available": round(cpus * idle_ratio, 2),
        "ram_total_mib": memory["MemTotal"],
        "ram_available_mib": memory["MemAvailable"],
        "gpus": [],
        "gpu_probe_error": None,
    }
    try:
        rows = nvidia_query(
            "--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu,mig.mode.current"
        )
        processes = gpu_processes()
        for row in rows:
            index, gpu_uuid, name, total, free, utilization, mig = row
            result["gpus"].append(
                {
                    "index": int(index),
                    "uuid": gpu_uuid,
                    "name": name,
                    "memory_total_mib": int(total),
                    "memory_free_mib": int(free),
                    "utilization": int(utilization),
                    "mig_enabled": mig == "Enabled",
                    "processes": [p for p in processes if p["uuid"] == gpu_uuid],
                }
            )
    except FileNotFoundError:
        pass
    except (subprocess.SubprocessError, ValueError) as exc:
        result["gpus"] = []
        result["gpu_probe_error"] = f"GPU telemetry unavailable: {type(exc).__name__}"
    return result


def run_directory(request):
    run_id = request["run_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Invalid run id")
    root = Path(request["root"]).expanduser().resolve()
    return root / "runs" / run_id


def inspect_run(directory):
    result = read_json(directory / "result.json")
    if result:
        return result
    state = read_json(directory / "state.json")
    launcher = read_json(directory / "launcher.json")
    if state and alive(state.get("supervisor")):
        return {**state, "status": "running", "metrics": read_json(directory / "metrics.json")}
    if launcher and alive(launcher.get("supervisor")):
        return {"status": "starting"}
    if state or launcher:
        return {
            "status": "lost",
            "error": "Worker disappeared without a durable exit record",
            "state": state,
            "leases_retained": True,
        }
    return {"status": "missing"}


def launch(request):
    directory = run_directory(request)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_json(directory / "spec.json")
        if previous is not None and previous != request["spec"]:
            raise ValueError("Run id already exists with a different command specification")
        status = inspect_run(directory)
        if status["status"] != "missing":
            return status
        if previous is None:
            atomic_json(directory / "spec.json", request["spec"])
        with (directory / "worker.log").open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--supervise", str(directory)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        atomic_json(directory / "launcher.json", {"supervisor": identity(proc.pid)})
    return {"status": "starting"}


def terminate_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def create_terminal(directory):
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is required on the execution server")
    session = "deepqueue-" + directory.name
    output = directory / "output.log"
    output.touch(mode=0o600)
    command = shlex.join(
        [sys.executable, str(Path(__file__).resolve()), "--follow", str(directory)]
    )
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "train",
            "-x",
            "160",
            "-y",
            "48",
            command,
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    subprocess.run(
        ["tmux", "set-window-option", "-t", session, "remain-on-exit", "on"],
        capture_output=True,
        check=True,
        timeout=10,
    )
    record = {
        "session": session,
        "window": "train",
        "target": session,
        "attach_command": shlex.join(["tmux", "attach-session", "-t", session]),
    }
    atomic_json(directory / "terminal.json", record)
    return record


def terminal(directory):
    record = read_json(directory / "terminal.json")
    if not record:
        return {"available": False}
    available = (
        bool(shutil.which("tmux"))
        and subprocess.run(
            ["tmux", "has-session", "-t", record["session"]],
            capture_output=True,
            timeout=10,
        ).returncode
        == 0
    )
    return {**record, "available": available}


def follow_output(directory):
    owner = read_json(directory / "state.json")["supervisor"]
    with (directory / "output.log").open("rb") as output:
        while True:
            finished = not alive(owner)
            while chunk := output.read(65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            if finished:
                pane = os.environ.get("TMUX_PANE", "")
                if re.fullmatch(r"%[0-9]+", pane):
                    # Retain the last log page without an idle follower process.
                    subprocess.run(
                        ["tmux", "copy-mode", "-t", pane], capture_output=True, timeout=10
                    )
                return
            time.sleep(0.2)


def supervise(directory):
    with (directory / "execute.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # A surviving state file is a launch fence, even if its process has disappeared.
        if (directory / "state.json").exists() or (directory / "result.json").exists():
            return
        started = time.time()
        state = {"status": "starting", "supervisor": identity(os.getpid()), "started_at": started}
        atomic_json(directory / "state.json", state)
        child = None
        tree, previous_subreaper = None, None
        try:
            spec = read_json(directory / "spec.json")
            if (directory / "cancel").exists():
                atomic_json(
                    directory / "result.json",
                    {"status": "cancelled", "exit_code": None, "finished_at": time.time()},
                )
                return
            environment = os.environ.copy()
            environment.update(spec.get("env", {}))
            environment.setdefault("PYTHONUNBUFFERED", "1")
            gpu_ids = spec["gpu_uuids"]
            environment.update(
                {
                    "CUDA_VISIBLE_DEVICES": ",".join(gpu_ids),
                    "NVIDIA_VISIBLE_DEVICES": ",".join(gpu_ids) if gpu_ids else "void",
                    "DEEPQUEUE_RUN_ID": directory.name,
                    "DEEPQUEUE_RUN_DIR": str(directory / "config"),
                }
            )
            config_root = directory / "config"
            config_root.mkdir(exist_ok=True, mode=0o700)
            for item in spec.get("launch_files", []):
                relative = Path(item["path"])
                if relative.is_absolute() or any(
                    p in ("", ".", "..") for p in item["path"].split("/")
                ):
                    raise ValueError("Invalid launch configuration path")
                target = config_root / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if (
                    not target.resolve().is_relative_to(config_root.resolve())
                    or target.is_symlink()
                ):
                    raise ValueError("Launch configuration cannot escape its run directory")
                target.write_text(item["content"], encoding="utf-8")
            if spec.get("tmux", False):
                state["terminal"] = create_terminal(directory)
                atomic_json(directory / "state.json", state)
            if (directory / "cancel").exists():
                atomic_json(
                    directory / "result.json",
                    {"status": "cancelled", "not_launched": True, "finished_at": time.time()},
                )
                return
            with (directory / "output.log").open("ab", buffering=0) as output:
                tree = ProcessTree()
                previous_subreaper = subreaper(True)
                child = subprocess.Popen(
                    ["/bin/bash", "-lc", spec["command"]],
                    cwd=spec["cwd"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                state.update({"status": "running", "child": identity(child.pid)})
                atomic_json(directory / "state.json", state)
                metrics = {"peak_ram_mib": 0.0, "gpu_peak_memory_mib": {}, "gpu_samples": 0}
                stopping, stop_at, last_sample = None, None, 0
                first_gpu_activity, observed_gpus, binding_error = None, set(), None
                while child.poll() is None:
                    now = time.time()
                    expired = (
                        spec.get("timeout_seconds", 0) and now - started >= spec["timeout_seconds"]
                    )
                    if stopping is None and ((directory / "cancel").exists() or expired):
                        stopping = "timeout" if expired else "cancelled"
                        stop_at = now
                        tree.signal(signal.SIGTERM)
                    if stop_at is not None and now - stop_at >= 5:
                        tree.signal(signal.SIGKILL)
                    if now - last_sample >= 5:
                        members = tree.members()
                        metrics["peak_ram_mib"] = max(
                            metrics["peak_ram_mib"], sum(rss for _, rss in members) / 1048576
                        )
                        if gpu_ids:
                            try:
                                pids = {pid for pid, _ in members}
                                usage = {}
                                processes = gpu_processes()
                                for process in processes:
                                    if process["pid"] in pids:
                                        key = process["uuid"]
                                        usage[key] = usage.get(key, 0) + process["memory_mib"]
                                for key, value in usage.items():
                                    peaks = metrics["gpu_peak_memory_mib"]
                                    peaks[key] = max(peaks.get(key, 0), value)
                                metrics["gpu_samples"] += 1
                                inaccessible = any(
                                    process["uuid"] in gpu_ids
                                    and not Path(f"/proc/{process['pid']}").exists()
                                    for process in processes
                                )
                                if inaccessible:
                                    warning = (
                                        "GPU process attribution unavailable: "
                                        "NVIDIA PIDs are not visible in /proc"
                                    )
                                    if metrics.get("gpu_attribution_error") != warning:
                                        output.write(("\n" + warning + "\n").encode())
                                    metrics["gpu_attribution_error"] = warning
                                else:
                                    metrics.pop("gpu_attribution_error", None)
                                observed_gpus.update(usage)
                                if usage and first_gpu_activity is None:
                                    first_gpu_activity = now
                                metrics["observed_gpu_uuids"] = sorted(observed_gpus)
                                if (
                                    stopping is None
                                    and first_gpu_activity is not None
                                    and not inaccessible
                                ):
                                    binding_error = gpu_binding_error(
                                        gpu_ids, usage, observed_gpus, now - first_gpu_activity
                                    )
                                    if binding_error:
                                        stopping, stop_at = "gpu_mapping", now
                                        output.write(("\n" + binding_error + "\n").encode())
                                        tree.signal(signal.SIGTERM)
                            except (OSError, ValueError, subprocess.SubprocessError):
                                pass
                        metrics["elapsed_seconds"] = now - started
                        atomic_json(directory / "metrics.json", metrics)
                        last_sample = now
                    time.sleep(0.2)
                exit_code = child.wait()
            # Reap all training descendants, including separate DDP sessions.
            tree.signal(signal.SIGKILL)
            deadline = time.monotonic() + 5
            while tree.members() and time.monotonic() < deadline:
                tree.signal(signal.SIGKILL)
                tree.reap()
                time.sleep(0.1)
            tree.reap()
            if tree.members():
                raise RuntimeError("Command descendants still exist after termination")
            metrics["peak_ram_mib"] = max(
                metrics["peak_ram_mib"],
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024,
            )
            metrics["elapsed_seconds"] = time.time() - started
            status = "cancelled" if stopping == "cancelled" else "succeeded"
            if exit_code != 0 or stopping in ("timeout", "gpu_mapping"):
                status = "cancelled" if stopping == "cancelled" else "failed"
            atomic_json(
                directory / "result.json",
                {
                    "status": status,
                    "exit_code": exit_code,
                    "started_at": started,
                    "finished_at": time.time(),
                    "metrics": metrics,
                    "error": "Runtime limit exceeded" if stopping == "timeout" else binding_error,
                    "failure_kind": "gpu_mapping" if binding_error else None,
                    "log_path": str(directory / "output.log"),
                    "gpu_uuids": gpu_ids,
                    "terminal": state.get("terminal"),
                },
            )
        except BaseException as exc:
            if child is not None:
                tree.signal(signal.SIGKILL)
                child.wait()
                tree.reap()
            status = "lost" if tree is not None and tree.members() else "failed"
            atomic_json(
                directory / "result.json",
                {
                    "status": status,
                    "exit_code": child.returncode if child else None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": time.time(),
                },
            )
        finally:
            if previous_subreaper is not None:
                subreaper(previous_subreaper)


def read_files(cwd, paths):
    root = Path(cwd).resolve(strict=True)
    files = []
    for name in paths[:16]:
        try:
            path = (root / name).resolve(strict=True)
            path.relative_to(root)
            if not path.is_file():
                raise ValueError("Not a regular file")
            with path.open("rb") as f:
                content = f.read(32769)
            files.append(
                {
                    "path": str(path),
                    "content": content[:32768].decode("utf-8", "replace"),
                    "truncated": len(content) > 32768,
                    "sha256_prefix": hashlib.sha256(content[:32768]).hexdigest(),
                }
            )
        except (OSError, ValueError) as exc:
            files.append({"path": name, "error": str(exc)})
    return files


def main(request):
    action = request["action"]
    if action == "probe":
        return probe()
    if action == "evidence":
        return {"files": read_files(request["cwd"], request.get("paths", []))}
    directory = run_directory(request)
    if action == "launch":
        return launch(request)
    if action == "inspect":
        return inspect_run(directory)
    if action == "terminal":
        return terminal(directory)
    if action == "cancel":
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / "cancel").touch(mode=0o600)
        return inspect_run(directory)
    if action == "logs":
        path = directory / "output.log"
        if not path.exists():
            return {"text": "", "bytes": 0, "truncated": False}
        limit = max(1, min(int(request.get("limit", 65536)), 1048576))
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - limit))
            content = f.read(limit)
        return {
            "text": content.decode("utf-8", "replace"),
            "bytes": size,
            "truncated": size > limit,
        }
    raise ValueError(f"Unknown worker action: {action}")


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise":
        supervise(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--follow":
        follow_output(Path(sys.argv[2]))
    else:
        try:
            print(json.dumps(main(json.load(sys.stdin)), allow_nan=False))
        except Exception as error:
            print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
            sys.exit(1)
