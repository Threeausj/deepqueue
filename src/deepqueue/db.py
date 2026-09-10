from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .batches import compile_batch
from .config import json_text
from .models import (
    ACTIVE,
    PRESTART,
    TERMINAL,
    AgentEfforts,
    AgentModels,
    JobSpec,
    JobUpdate,
    Resources,
    Server,
)

SCHEMA_VERSION = 7
BATCH_SCOPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_server_pauses (
    batch_name TEXT NOT NULL REFERENCES batches(name),
    server TEXT NOT NULL REFERENCES servers(name),
    paused INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(batch_name, server)
)
"""
SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS servers (
    name TEXT PRIMARY KEY, config TEXT NOT NULL, snapshot TEXT, error TEXT, updated_at REAL
)
""",
    """
CREATE TABLE IF NOT EXISTS batches (
    name TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, created_at REAL NOT NULL,
    job_ids TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0
)
""",
    """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, spec TEXT NOT NULL, server TEXT NOT NULL REFERENCES servers(name),
    status TEXT NOT NULL, resources TEXT, estimate TEXT, reason TEXT,
    priority INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0, run_id TEXT,
    archive_status TEXT NOT NULL, analysis TEXT, idempotency_key TEXT UNIQUE,
    spec_hash TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
    batch_name TEXT REFERENCES batches(name), completion_mode TEXT NOT NULL DEFAULT 'finish',
    launch_status TEXT NOT NULL DEFAULT 'skipped', launch_retries INTEGER NOT NULL DEFAULT 0
)
""",
    "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, server)",
    "CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_name)",
    """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), server TEXT NOT NULL,
    allocation TEXT NOT NULL, resources TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, finished_at REAL, result TEXT, launch_plan TEXT, recovery TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS gpu_leases (
    server TEXT NOT NULL, uuid TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id),
    PRIMARY KEY(server, uuid)
)
""",
    """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), phase TEXT NOT NULL,
    status TEXT NOT NULL, model TEXT NOT NULL, thread_id TEXT, deep_link TEXT, turn_id TEXT,
    created_at REAL NOT NULL, finished_at REAL, response TEXT, error TEXT,
    delivery_id TEXT, request_sent INTEGER NOT NULL DEFAULT 0, effort TEXT, run_id TEXT
)
""",
    "CREATE INDEX IF NOT EXISTS agent_runs_thread ON agent_runs(thread_id,status)",
    """CREATE TABLE IF NOT EXISTS resource_holds (
        run_id TEXT PRIMARY KEY REFERENCES runs(id), job_id TEXT NOT NULL REFERENCES jobs(id),
        expires_at REAL NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS jobs_parent ON jobs(json_extract(spec,'$.parent_job_id'))
        WHERE json_extract(spec,'$.parent_job_id') IS NOT NULL""",
    """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT REFERENCES jobs(id),
    at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
)
""",
)

JOB_SELECT = """
SELECT j.*, COALESCE(bp.paused, b.paused, 0) AS batch_paused,
       COALESCE(json_extract(s.config, '$.enabled'), 1) AS server_enabled
FROM jobs j LEFT JOIN batches b ON b.name=j.batch_name JOIN servers s ON s.name=j.server
LEFT JOIN batch_server_pauses bp ON bp.batch_name=j.batch_name AND bp.server=j.server
"""


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in (
        "spec",
        "config",
        "snapshot",
        "resources",
        "estimate",
        "analysis",
        "allocation",
        "result",
        "response",
        "data",
        "launch_plan",
        "recovery",
    ):
        if key in result and result[key] is not None:
            result[key] = json.loads(result[key])
    for key in ("paused", "batch_paused", "server_enabled"):
        if key in result:
            result[key] = bool(result[key])
    if "batch_paused" in result:
        result["pause_sources"] = (
            [
                scope
                for scope, held in (
                    ("job", result["paused"]),
                    ("batch", result["batch_paused"]),
                    ("server", not result["server_enabled"]),
                )
                if held
            ]
            if result["status"] in PRESTART
            else []
        )
    return result


class Database:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "queue.sqlite3"

    def initialize(self):
        with self.connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("BEGIN IMMEDIATE")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(f"Database version {version} is newer than this DeepQueue")
            if version == 0:
                for statement in SCHEMA:
                    con.execute(statement)
            elif version == 1:
                con.execute("ALTER TABLE batches ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
                con.execute("ALTER TABLE jobs ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
                con.execute("ALTER TABLE jobs ADD COLUMN batch_name TEXT REFERENCES batches(name)")
                # The batch manifest owns membership; a standalone job can share its group label.
                for batch in con.execute("SELECT name,job_ids FROM batches").fetchall():
                    for job_id in json.loads(batch["job_ids"]).values():
                        changed = con.execute(
                            "UPDATE jobs SET batch_name=? WHERE id=? AND batch_name IS NULL",
                            (batch["name"], job_id),
                        ).rowcount
                        if changed != 1:
                            raise ValueError(
                                f"Invalid batch membership in {batch['name']}: {job_id}"
                            )
                con.execute("CREATE INDEX jobs_batch ON jobs(batch_name)")
            if version in (1, 2):
                con.execute("ALTER TABLE agent_runs ADD COLUMN delivery_id TEXT")
                con.execute(
                    "ALTER TABLE agent_runs ADD COLUMN request_sent INTEGER NOT NULL DEFAULT 0"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS agent_runs_thread ON agent_runs(thread_id,status)"
                )
            if version in (1, 2, 3):
                con.execute(
                    "ALTER TABLE jobs ADD COLUMN completion_mode TEXT NOT NULL DEFAULT 'finish'"
                )
                con.execute(
                    "ALTER TABLE jobs ADD COLUMN launch_status TEXT NOT NULL DEFAULT 'skipped'"
                )
                con.execute("ALTER TABLE runs ADD COLUMN launch_plan TEXT")
                for statement in SCHEMA:
                    con.execute(statement)
            if version in (1, 2, 3, 4) and "effort" not in {
                row[1] for row in con.execute("PRAGMA table_info(agent_runs)")
            }:
                con.execute("ALTER TABLE agent_runs ADD COLUMN effort TEXT")
            for table, column, definition in (
                ("jobs", "launch_retries", "INTEGER NOT NULL DEFAULT 0"),
                ("runs", "recovery", "TEXT"),
                ("agent_runs", "run_id", "TEXT"),
            ):
                if column not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            con.execute(BATCH_SCOPE_SCHEMA)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.path.chmod(0o600)

    def schema_version(self):
        with self.connection() as con:
            return con.execute("PRAGMA user_version").fetchone()[0]

    def ensure_schema(self):
        if self.schema_version() != SCHEMA_VERSION:
            self.initialize()

    @contextmanager
    def connection(self, *, write=False):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def event(con, job_id, kind, data):
        con.execute(
            "INSERT INTO events(job_id,at,kind,data) VALUES(?,?,?,?)",
            (job_id, time.time(), kind, json_text(data)),
        )

    def add_server(self, server: Server):
        with self.connection(write=True) as con:
            if con.execute("SELECT 1 FROM servers WHERE name=?", (server.name,)).fetchone():
                raise ValueError(f"Server already exists: {server.name}")
            order = con.execute(
                "SELECT COALESCE(MAX(json_extract(config, '$.sort_order')), 0) + 1 FROM servers"
            ).fetchone()[0]
            server = server.model_copy(update={"sort_order": order})
            con.execute(
                "INSERT INTO servers(name,config) VALUES(?,?)",
                (server.name, server.model_dump_json()),
            )

    def servers(self):
        with self.connection() as con:
            return [
                decode(row)
                for row in con.execute(
                    "SELECT * FROM servers "
                    "ORDER BY COALESCE(json_extract(config, '$.sort_order'), 0), name"
                )
            ]

    def reorder_servers(self, names):
        with self.connection(write=True) as con:
            current = {row["name"] for row in con.execute("SELECT name FROM servers")}
            if len(names) != len(set(names)) or set(names) != current:
                raise ValueError("服务器列表已变化或顺序包含重复项，请刷新后重试")
            for position, name in enumerate(names, 1):
                con.execute(
                    "UPDATE servers SET config=json_set(config, '$.sort_order', ?) WHERE name=?",
                    (position, name),
                )
            self.event(con, None, "servers_reordered", {"servers": names})

    def update_server(self, name, changes, *, expected_connection=None, check_only=False):
        connection_fields = {
            "host",
            "port",
            "username",
            "key_file",
            "password_ref",
            "passphrase_ref",
        }
        if not set(changes) <= connection_fields | {"display_name", "max_running"}:
            raise ValueError("Unsupported server setting")
        with self.connection(write=True) as con:
            row = con.execute("SELECT config FROM servers WHERE name=?", (name,)).fetchone()
            if not row:
                raise ValueError(f"Unknown server: {name}")
            current = Server.model_validate_json(row["config"])
            if expected_connection is not None and any(
                getattr(current, key) != value for key, value in expected_connection.items()
            ):
                raise ValueError("服务器连接设置已被修改，请刷新后重试")
            updated = Server.model_validate({**current.model_dump(), **changes})
            connection_changed = any(
                getattr(current, key) != getattr(updated, key) for key in connection_fields
            )
            if connection_changed:
                if current.kind != "ssh":
                    raise ValueError("本机服务器不支持 SSH 连接设置")
                busy = con.execute(
                    "SELECT 1 FROM jobs j WHERE j.server=? AND ("
                    "j.status IN ('estimating', 'starting', 'running', 'lost') OR "
                    "EXISTS (SELECT 1 FROM agent_runs a "
                    "WHERE a.job_id=j.id AND a.status='running') "
                    "OR EXISTS (SELECT 1 FROM resource_holds h WHERE h.job_id=j.id)) LIMIT 1",
                    (name,),
                ).fetchone()
                if busy:
                    raise ValueError("此服务器仍有实验或 Agent 在运行，请结束后再修改连接信息")
            if check_only:
                return
            con.execute(
                "UPDATE servers SET config=?, snapshot=CASE WHEN ? THEN NULL ELSE snapshot END, "
                "error=CASE WHEN ? THEN NULL ELSE error END, "
                "updated_at=CASE WHEN ? THEN NULL ELSE updated_at END WHERE name=?",
                (
                    updated.model_dump_json(),
                    connection_changed,
                    connection_changed,
                    connection_changed,
                    name,
                ),
            )
            self.event(con, None, "server_updated", {"server": name, "fields": sorted(changes)})

    def server(self, name):
        with self.connection() as con:
            row = decode(con.execute("SELECT * FROM servers WHERE name=?", (name,)).fetchone())
        if not row:
            raise ValueError(f"Unknown server: {name}")
        return row

    def enable_server(self, name, enabled):
        with self.connection(write=True) as con:
            row = con.execute("SELECT config FROM servers WHERE name=?", (name,)).fetchone()
            if not row:
                raise ValueError(f"Unknown server: {name}")
            server = Server.model_validate_json(row["config"])
            if server.enabled == enabled:
                return
            server.enabled = enabled
            con.execute(
                "UPDATE servers SET config=? WHERE name=?", (server.model_dump_json(), name)
            )
            self.event(
                con, None, "server_resumed" if enabled else "server_paused", {"server": name}
            )

    def set_server_callback(self, name, socket):
        with self.connection(write=True) as con:
            row = con.execute("SELECT config FROM servers WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown server: {name}")
            server = Server.model_validate_json(row["config"])
            server.return_agent_socket = socket
            con.execute(
                "UPDATE servers SET config=? WHERE name=?", (server.model_dump_json(), name)
            )

    def set_server_codex(self, name, config):
        with self.connection(write=True) as con:
            row = con.execute("SELECT config FROM servers WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown server: {name}")
            server = Server.model_validate_json(row["config"])
            server.codex = config
            con.execute(
                "UPDATE servers SET config=? WHERE name=?", (server.model_dump_json(), name)
            )
            self.event(con, None, "server_codex_configured", {"server": name})

    @staticmethod
    def _server_matches(con, expected):
        row = con.execute("SELECT config FROM servers WHERE name=?", (expected.name,)).fetchone()
        if not row:
            return False
        actual = Server.model_validate_json(row["config"])
        metadata = {"display_name", "sort_order"}
        return actual.model_dump(exclude=metadata) == expected.model_dump(exclude=metadata)

    def snapshot(self, name, snapshot=None, error=None, *, expected_server=None):
        with self.connection(write=True) as con:
            if expected_server is not None and (
                name != expected_server.name or not self._server_matches(con, expected_server)
            ):
                return False
            con.execute(
                "UPDATE servers SET snapshot=?,error=?,updated_at=? WHERE name=?",
                (json_text(snapshot) if snapshot else None, error, time.time(), name),
            )
        return True

    def submit(self, spec: JobSpec):
        with self.connection(write=True) as con:
            job_id = self._submit(con, spec)
        return self.job(job_id)

    def _submit(self, con, spec, job_id=None, batch_ids=(), batch_name=None):
        import hashlib

        if spec.baseline_job_id:
            baseline = con.execute(
                "SELECT server,status,run_id FROM jobs WHERE id=?", (spec.baseline_job_id,)
            ).fetchone()
            if (
                not baseline
                or baseline["server"] != spec.server
                or baseline["status"] not in ("succeeded", "failed")
                or not baseline["run_id"]
            ):
                raise ValueError("Baseline must be an executed experiment on the same server")
        if spec.parent_job_id:
            parent = decode(
                con.execute("SELECT * FROM jobs WHERE id=?", (spec.parent_job_id,)).fetchone()
            )
            if (
                not parent
                or parent["completion_mode"] != "improve"
                or parent["status"] not in ("succeeded", "failed")
                or parent["cancel_requested"]
            ):
                raise ValueError("Parent has not authorized an improvement after execution")
            if (
                spec.source_thread_id != parent["spec"].get("source_thread_id")
                or spec.server != parent["server"]
            ):
                raise ValueError(
                    "An improvement must use its parent's source conversation and server"
                )
            round_number = parent["spec"].get("improvement_round", 0) + 1
            limit = parent["spec"].get("max_improvement_rounds", 1)
            if round_number > limit:
                raise ValueError("Improvement round limit reached")
            spec = spec.model_copy(
                update={
                    "priority": 100,
                    "improvement_round": round_number,
                    "max_improvement_rounds": limit,
                    "completion_mode": "improve" if round_number < limit else "finish",
                    "idempotency_key": f"improvement:{spec.parent_job_id}",
                    "agent_models": AgentModels.model_validate(
                        {
                            **parent["spec"].get("agent_models", {}),
                            **spec.agent_models.model_dump(exclude_unset=True),
                        }
                    ),
                    "agent_efforts": AgentEfforts.model_validate(
                        {
                            **parent["spec"].get("agent_efforts", {}),
                            **spec.agent_efforts.model_dump(exclude_unset=True),
                        }
                    ),
                }
            )
        elif spec.improvement_round:
            raise ValueError("improvement_round is assigned through parent_job_id")
        canonical_spec = spec.model_dump()
        for field in ("agent_models", "agent_efforts"):
            canonical_spec[field] = getattr(spec, field).model_dump(exclude_none=True)
            if not canonical_spec[field]:
                canonical_spec.pop(field)
        if not canonical_spec["parameters"]:
            canonical_spec.pop("parameters")
        if not canonical_spec["source_thread_id"]:
            canonical_spec.pop("source_thread_id")
        for field, default in {
            "completion_mode": "finish",
            "max_improvement_rounds": 1,
            "improvement_round": 0,
            "parent_job_id": None,
            "baseline_job_id": None,
            "launch_max_retries": None,
            "launch_agent": True,
            "tmux": True,
        }.items():
            if canonical_spec[field] == default:
                canonical_spec.pop(field)
        canonical = json.dumps(canonical_spec, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        if spec.idempotency_key:
            old = decode(
                con.execute(
                    "SELECT * FROM jobs WHERE idempotency_key=?", (spec.idempotency_key,)
                ).fetchone()
            )
            if old:
                if old["spec_hash"] != fingerprint:
                    raise ValueError("Idempotency key already used with a different job spec")
                return old["id"]
        if not con.execute("SELECT 1 FROM servers WHERE name=?", (spec.server,)).fetchone():
            raise ValueError(f"Unknown server: {spec.server}")
        for dependency in spec.depends_on:
            if (
                dependency not in batch_ids
                and not con.execute("SELECT 1 FROM jobs WHERE id=?", (dependency,)).fetchone()
            ):
                raise ValueError(f"Unknown dependency: {dependency}")
        job_id = job_id or uuid.uuid4().hex
        now = time.time()
        status = "queued" if spec.skip_estimate else "pending"
        resources = spec.resources.model_dump_json() if spec.resources else None
        con.execute(
            """INSERT INTO jobs
            (id,spec,server,status,resources,priority,created_at,updated_at,
             archive_status,idempotency_key,spec_hash,batch_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                spec.model_dump_json(),
                spec.server,
                status,
                resources,
                spec.priority,
                now,
                now,
                "pending" if spec.archive else "skipped",
                spec.idempotency_key,
                fingerprint,
                batch_name,
            ),
        )
        self.event(con, job_id, "submitted", {"status": status})
        con.execute("UPDATE jobs SET completion_mode=? WHERE id=?", (spec.completion_mode, job_id))
        return job_id

    def submit_batch(self, manifest):
        plan = compile_batch(manifest)
        with self.connection(write=True) as con:
            previous = self._check_batch(con, plan)
            if previous:
                return {
                    "name": plan.name,
                    "jobs": json.loads(previous["job_ids"]),
                    "existing": True,
                }
            mapping = {key: uuid.uuid4().hex for key in plan.experiments}
            batch_ids = set(mapping.values())
            con.execute(
                "INSERT INTO batches(name,manifest_hash,created_at,job_ids) VALUES(?,?,?,?)",
                (plan.name, plan.digest, time.time(), json_text(mapping)),
            )
            for key in plan.order:
                spec = plan.experiments[key].model_copy(
                    update={
                        "depends_on": [
                            mapping.get(dep, dep) for dep in plan.experiments[key].depends_on
                        ],
                    }
                )
                self._submit(con, spec, mapping[key], batch_ids, plan.name)
        return {"name": plan.name, "jobs": mapping, "existing": False}

    @staticmethod
    def _check_batch(con, plan):
        previous = con.execute("SELECT * FROM batches WHERE name=?", (plan.name,)).fetchone()
        if previous:
            if previous["manifest_hash"] != plan.digest:
                raise ValueError("Batch name already exists with a different manifest")
            return previous
        servers = {row[0] for row in con.execute("SELECT name FROM servers")}
        external = {
            dep
            for spec in plan.experiments.values()
            for dep in spec.depends_on
            if dep not in plan.experiments
        }
        for dependency in external:
            if not con.execute("SELECT 1 FROM jobs WHERE id=?", (dependency,)).fetchone():
                raise ValueError(f"Unknown dependency: {dependency}")
        for key, spec in plan.experiments.items():
            if spec.server not in servers:
                raise ValueError(f"Unknown server: {spec.server} (experiment {key})")
            if con.execute(
                "SELECT 1 FROM jobs WHERE idempotency_key=?", (spec.idempotency_key,)
            ).fetchone():
                raise ValueError(f"Batch idempotency key already belongs to an existing job: {key}")
        return None

    def preview_batch(self, manifest):
        plan = compile_batch(manifest)
        with self.connection() as con:
            con.execute("BEGIN")
            previous = self._check_batch(con, plan)
        return {
            "name": plan.name,
            "manifest_hash": plan.digest,
            "existing": previous is not None,
            "experiment_count": len(plan.experiments),
            "estimation_count": sum(not spec.skip_estimate for spec in plan.experiments.values()),
            "dependency_order": plan.order,
            "experiments": [
                {"key": key, **spec.model_dump()} for key, spec in plan.experiments.items()
            ],
        }

    def batch(self, name, server=None):
        with self.connection() as con:
            con.execute("BEGIN")
            batch = self._batch(con, name)
            result = {
                "name": name,
                "created_at": batch["created_at"],
                "paused": bool(batch["paused"]),
                "experiments": [],
            }
            jobs = {
                row["id"]: decode(row)
                for row in con.execute(JOB_SELECT + " WHERE j.batch_name=?", (name,))
            }
            for key, job_id in json.loads(batch["job_ids"]).items():
                job = jobs[job_id]
                if server is not None and job["server"] != server:
                    continue
                job["key"] = key
                result["experiments"].append(job)
            if server is not None:
                if not result["experiments"]:
                    raise ValueError(f"Batch {name} has no experiments on server {server}")
                result["server"] = server
                result["paused"] = result["experiments"][0]["batch_paused"]
            result["events"] = [
                decode(row)
                for row in con.execute(
                    "SELECT * FROM events WHERE job_id IS NULL AND json_extract(data, '$.batch')=? "
                    + (
                        "AND (json_extract(data, '$.server') IS NULL OR "
                        "json_extract(data, '$.server')=?) "
                        if server is not None
                        else ""
                    )
                    + "ORDER BY id",
                    (name, server) if server is not None else (name,),
                )
            ]
        return result

    def batches(self, server=None):
        with self.connection() as con:
            if server is not None:
                return [
                    decode(row)
                    for row in con.execute(
                        "SELECT b.name,b.created_at,COALESCE(bp.paused,b.paused) AS paused "
                        "FROM batches b LEFT JOIN batch_server_pauses bp "
                        "ON bp.batch_name=b.name AND bp.server=? "
                        "WHERE EXISTS (SELECT 1 FROM jobs j WHERE j.batch_name=b.name "
                        "AND j.server=?) ORDER BY b.created_at",
                        (server, server),
                    )
                ]
            return [
                decode(r)
                for r in con.execute(
                    "SELECT name,created_at,paused FROM batches ORDER BY created_at"
                )
            ]

    def jobs(self, statuses=None, server=None, limit=1000, *, dispatchable=False):
        where, params = [], []
        if statuses:
            where.append("j.status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if server:
            where.append("j.server=?")
            params.append(server)
        if dispatchable:
            where.append("j.paused=0 AND COALESCE(bp.paused,b.paused,0)=0")
        query = JOB_SELECT + (" WHERE " + " AND ".join(where) if where else "")
        query += " ORDER BY j.created_at,j.id LIMIT ?"
        with self.connection() as con:
            return [decode(r) for r in con.execute(query, [*params, limit])]

    def job(self, job_id):
        with self.connection() as con:
            row = decode(con.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone())
        if not row:
            raise ValueError(f"Unknown job: {job_id}")
        return row

    def detail(self, job_id):
        job = self.job(job_id)
        with self.connection() as con:
            job["runs"] = [
                decode(r)
                for r in con.execute(
                    "SELECT * FROM runs WHERE job_id=? ORDER BY created_at", (job_id,)
                )
            ]
            job["agents"] = [
                decode(r)
                for r in con.execute(
                    "SELECT * FROM agent_runs WHERE job_id=? ORDER BY created_at", (job_id,)
                )
            ]
            job["events"] = [
                decode(r)
                for r in con.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,))
            ]
            hold = con.execute("SELECT * FROM resource_holds WHERE job_id=?", (job_id,)).fetchone()
            child = con.execute(
                "SELECT id,status FROM jobs WHERE json_extract(spec,'$.parent_job_id')=?",
                (job_id,),
            ).fetchone()
            job["resource_hold"] = dict(hold) if hold else None
            job["improvement_child"] = dict(child) if child else None
        return job

    def comparison_job(self, job):
        """Choose an actual earlier execution, preferring an explicit baseline or parent."""
        baseline_id = job["spec"].get("baseline_job_id") or job["spec"].get("parent_job_id")
        with self.connection() as con:
            if baseline_id:
                row = con.execute(
                    "SELECT * FROM jobs WHERE id=? AND id!=? AND server=? "
                    "AND status IN ('succeeded','failed') AND run_id IS NOT NULL",
                    (baseline_id, job["id"], job["server"]),
                ).fetchone()
                selection = "explicit" if job["spec"].get("baseline_job_id") else "parent"
            else:
                current = con.execute(
                    "SELECT created_at FROM runs WHERE id=?", (job["run_id"],)
                ).fetchone()
                cutoff = current["created_at"] if current else job["created_at"]
                row = con.execute(
                    "SELECT j.* FROM jobs j JOIN runs r ON j.run_id=r.id "
                    "WHERE j.id!=? AND j.server=? AND json_extract(j.spec,'$.cwd')=? "
                    "AND j.status IN ('succeeded','failed') AND r.finished_at<=? "
                    "ORDER BY r.finished_at DESC,j.id DESC LIMIT 1",
                    (job["id"], job["server"], job["spec"]["cwd"], cutoff),
                ).fetchone()
                selection = "previous_same_project"
        return (decode(row), selection) if row else (None, None)

    def transition(self, job_id, expected, status, reason=None):
        with self.connection(write=True) as con:
            changed = con.execute(
                "UPDATE jobs SET status=?,reason=?,updated_at=? WHERE id=? AND status=?",
                (status, reason, time.time(), job_id, expected),
            ).rowcount
            if changed:
                self.event(con, job_id, status, {"reason": reason})
        return bool(changed)

    def reason(self, job_id, reason):
        with self.connection(write=True) as con:
            old = con.execute("SELECT reason FROM jobs WHERE id=?", (job_id,)).fetchone()
            if old and old[0] != reason:
                con.execute(
                    "UPDATE jobs SET reason=?,updated_at=? WHERE id=?",
                    (reason, time.time(), job_id),
                )
                self.event(con, job_id, "waiting", {"reason": reason})

    def approve(self, job_id, resources: Resources | None = None):
        with self.connection(write=True) as con:
            job = decode(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job or job["status"] != "needs_review":
                raise ValueError("Only needs_review jobs can be approved")
            resource_data = resources.model_dump() if resources else job["resources"]
            if not resource_data:
                raise ValueError("Approval requires explicit resources")
            con.execute(
                "UPDATE jobs SET status='queued',resources=?,reason=NULL,updated_at=? WHERE id=?",
                (json_text(resource_data), time.time(), job_id),
            )
            self.event(con, job_id, "approved", {"resources": resource_data})

    def cancel(self, job_id):
        with self.connection(write=True) as con:
            job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise ValueError(f"Unknown job: {job_id}")
            self._end_improvements(con, job_id, cancel_running=True)
            self._cancel(con, job)

    def _cancel(self, con, job):
        if job["status"] in TERMINAL or job["cancel_requested"]:
            return
        active = job["status"] in ACTIVE
        con.execute(
            "UPDATE jobs SET cancel_requested=1,status=?,updated_at=? WHERE id=?",
            (job["status"] if active else "cancelled", time.time(), job["id"]),
        )
        self.event(con, job["id"], "cancel_requested" if active else "cancelled", {})

    @staticmethod
    def _batch(con, name):
        row = con.execute("SELECT * FROM batches WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError(f"Unknown batch: {name}")
        return row

    @staticmethod
    def _prestart(con, job_id):
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown job: {job_id}")
        if row["status"] not in PRESTART:
            raise ValueError("This control only applies to jobs that have not started")
        return row

    def set_paused(self, job_id, paused):
        with self.connection(write=True) as con:
            job = self._prestart(con, job_id)
            if bool(job["paused"]) == paused:
                return
            con.execute(
                "UPDATE jobs SET paused=?,updated_at=? WHERE id=?",
                (int(paused), time.time(), job_id),
            )
            self.event(con, job_id, "paused" if paused else "resumed", {})

    def set_batch_paused(self, name, paused, server=None):
        with self.connection(write=True) as con:
            batch = self._batch(con, name)
            if server is None:
                overrides = con.execute(
                    "DELETE FROM batch_server_pauses WHERE batch_name=?", (name,)
                ).rowcount
                if bool(batch["paused"]) == paused and not overrides:
                    return
                con.execute("UPDATE batches SET paused=? WHERE name=?", (int(paused), name))
            else:
                self._batch_jobs(con, name, server)
                con.execute(
                    "INSERT INTO batch_server_pauses(batch_name,server,paused) VALUES(?,?,?) "
                    "ON CONFLICT(batch_name,server) DO UPDATE SET paused=excluded.paused",
                    (name, server, int(paused)),
                )
            self.event(
                con,
                None,
                "batch_paused" if paused else "batch_resumed",
                {"batch": name, **({"server": server} if server is not None else {})},
            )

    def _batch_jobs(self, con, name, server=None):
        self._batch(con, name)
        rows = con.execute(
            "SELECT * FROM jobs WHERE batch_name=?"
            + (" AND server=?" if server is not None else ""),
            (name, server) if server is not None else (name,),
        ).fetchall()
        if server is not None and not rows:
            raise ValueError(f"Batch {name} has no experiments on server {server}")
        return rows

    @staticmethod
    def _priority(value):
        if type(value) is not int or not -100 <= value <= 100:
            raise ValueError("Priority must be an integer between -100 and 100")

    def _set_priority(self, con, job, priority):
        if job["priority"] == priority:
            return False
        con.execute(
            "UPDATE jobs SET priority=?,updated_at=? WHERE id=?",
            (priority, time.time(), job["id"]),
        )
        self.event(con, job["id"], "priority_changed", {"old": job["priority"], "new": priority})
        return True

    def set_priority(self, job_id, priority):
        self._priority(priority)
        with self.connection(write=True) as con:
            self._set_priority(con, self._prestart(con, job_id), priority)

    def set_batch_priority(self, name, priority, server=None):
        self._priority(priority)
        with self.connection(write=True) as con:
            jobs = self._batch_jobs(con, name, server)
            eligible = [job for job in jobs if job["status"] in PRESTART]
            updated = sum(self._set_priority(con, job, priority) for job in eligible)
            result = {"updated": updated, "eligible": len(eligible), "total": len(jobs)}
            if updated:
                self.event(
                    con,
                    None,
                    "batch_priority_changed",
                    {
                        "batch": name,
                        "priority": priority,
                        **result,
                        **({"server": server} if server is not None else {}),
                    },
                )
        return result

    def cancel_batch(self, name, server=None):
        with self.connection(write=True) as con:
            jobs = self._batch_jobs(con, name, server)
            for job in jobs:
                self._end_improvements(con, job["id"], cancel_running=True)
                self._cancel(con, job)
            self.event(
                con,
                None,
                "batch_cancel_requested",
                {"batch": name, **({"server": server} if server is not None else {})},
            )

    def leases(self, server):
        with self.connection() as con:
            return [
                dict(r) for r in con.execute("SELECT * FROM gpu_leases WHERE server=?", (server,))
            ]

    def active_runs(self, server):
        with self.connection() as con:
            return [
                decode(r)
                for r in con.execute(
                    "SELECT * FROM runs WHERE server=? AND (finished_at IS NULL OR id IN "
                    "(SELECT run_id FROM resource_holds))",
                    (server,),
                )
            ]

    def reserve(self, job_id, allocation, *, expected_server=None):
        with self.connection(write=True) as con:
            job = decode(con.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone())
            if (
                not job
                or job["status"] != "queued"
                or job["cancel_requested"]
                or job["pause_sources"]
            ):
                return None
            if expected_server is not None and (
                job["server"] != expected_server.name
                or not self._server_matches(con, expected_server)
            ):
                return None
            run_id = uuid.uuid4().hex
            parent_id = job["spec"].get("parent_job_id")
            if parent_id:
                self._release_hold(con, parent_id)
            con.execute(
                """INSERT INTO runs
                (id,job_id,server,allocation,resources,status,created_at) VALUES(?,?,?,?,?,?,?)""",
                (
                    run_id,
                    job_id,
                    job["server"],
                    json_text(allocation),
                    json_text(job["resources"]),
                    "starting",
                    time.time(),
                ),
            )
            try:
                for gpu in allocation:
                    con.execute(
                        "INSERT INTO gpu_leases(server,uuid,run_id) VALUES(?,?,?)",
                        (job["server"], gpu["uuid"], run_id),
                    )
            except sqlite3.IntegrityError:
                con.rollback()
                return None
            con.execute(
                "UPDATE jobs SET status='starting',run_id=?,reason=NULL,updated_at=? WHERE id=?",
                (run_id, time.time(), job_id),
            )
            con.execute(
                "UPDATE jobs SET launch_status=? WHERE id=?",
                (
                    "pending" if job["spec"].get("launch_agent", False) else "skipped",
                    job_id,
                ),
            )
            self.event(con, job_id, "reserved", {"run_id": run_id, "gpus": allocation})
        return run_id

    def run(self, run_id):
        with self.connection() as con:
            row = decode(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        if not row:
            raise ValueError(f"Unknown run: {run_id}")
        return row

    def retry_launch(self, job_id, run_id, result, kind, max_retries, *, allocation=None):
        if result["status"] != "failed" or kind not in ("oom", "gpu_mapping", "launch_config"):
            return None
        with self.connection(write=True) as con:
            job = decode(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job and job["spec"].get("launch_max_retries") is not None:
                max_retries = job["spec"]["launch_max_retries"]
            if (
                not job
                or job["run_id"] != run_id
                or job["status"] not in ("starting", "running")
                or job["cancel_requested"]
                or not job["spec"].get("launch_agent", False)
                or job["launch_retries"] >= max_retries
            ):
                return None
            previous = decode(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not previous["allocation"] or previous["finished_at"] is not None:
                return None
            allocation = previous["allocation"] if allocation is None else allocation
            next_id = uuid.uuid4().hex
            now = time.time()
            recovery = {
                "from_run_id": run_id,
                "kind": kind,
                "attempt": job["launch_retries"] + 1,
            }
            con.execute(
                "UPDATE runs SET status='failed',result=?,finished_at=? WHERE id=?",
                (json_text({**result, "failure_kind": kind}), now, run_id),
            )
            con.execute(
                "INSERT INTO runs"
                "(id,job_id,server,allocation,resources,status,created_at,recovery) "
                "VALUES(?,?,?,?,?,'starting',?,?)",
                (
                    next_id,
                    job_id,
                    job["server"],
                    json_text(allocation),
                    json_text(previous["resources"]),
                    now,
                    json_text(recovery),
                ),
            )
            # Transfer the reservation in the same transaction as the new execution attempt.
            con.execute("DELETE FROM gpu_leases WHERE run_id=?", (run_id,))
            try:
                for gpu in allocation:
                    con.execute(
                        "INSERT INTO gpu_leases(server,uuid,run_id) VALUES(?,?,?)",
                        (job["server"], gpu["uuid"], next_id),
                    )
            except sqlite3.IntegrityError:
                con.rollback()
                return None
            reason = f"自动修复 {recovery['attempt']}/{max_retries}: {kind}"
            con.execute(
                "UPDATE jobs SET status='starting',run_id=?,launch_status='pending',"
                "launch_retries=launch_retries+1,reason=?,updated_at=? WHERE id=?",
                (next_id, reason, now, job_id),
            )
            self.event(con, job_id, "launch_recovery", {**recovery, "run_id": next_id})
        return next_id

    def running(self, job_id):
        with self.connection(write=True) as con:
            job = decode(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job["status"] not in ("starting", "lost"):
                return
            con.execute(
                "UPDATE jobs SET status='running',reason=NULL,updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            con.execute("UPDATE runs SET status='running' WHERE id=?", (job["run_id"],))
            self.event(con, job_id, "running", {})

    def finish(self, job_id, result, *, resolve=False, hold_seconds=900, expected_run_id=None):
        status = result["status"]
        if status not in (*TERMINAL, "lost"):
            raise ValueError(f"Invalid result status: {status}")
        with self.connection(write=True) as con:
            job = decode(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if (
                not job
                or job["status"] not in ACTIVE
                or (expected_run_id and job["run_id"] != expected_run_id)
            ):
                return
            if resolve and job["status"] != "lost":
                raise ValueError("Only lost jobs can be resolved")
            con.execute(
                "UPDATE jobs SET status=?,reason=?,updated_at=? WHERE id=?",
                (status, result.get("error"), time.time(), job_id),
            )
            con.execute(
                "UPDATE runs SET status=?,result=?,finished_at=? WHERE id=?",
                (
                    status,
                    json_text(result),
                    None if status == "lost" else time.time(),
                    job["run_id"],
                ),
            )
            hold = (
                status in ("succeeded", "failed")
                and job["completion_mode"] == "improve"
                and not job["cancel_requested"]
            )
            if hold:
                con.execute(
                    "INSERT OR IGNORE INTO resource_holds VALUES(?,?,?)",
                    (job["run_id"], job_id, time.time() + hold_seconds),
                )
            elif status != "lost":
                con.execute("DELETE FROM gpu_leases WHERE run_id=?", (job["run_id"],))
            if job["status"] != status:
                self.event(con, job_id, "resolved" if resolve else status, result)

    @staticmethod
    def _release_hold(con, job_id):
        con.execute(
            "DELETE FROM gpu_leases WHERE run_id IN "
            "(SELECT run_id FROM resource_holds WHERE job_id=?)",
            (job_id,),
        )
        con.execute("DELETE FROM resource_holds WHERE job_id=?", (job_id,))

    def held_run(self, parent_id):
        with self.connection() as con:
            return decode(
                con.execute(
                    "SELECT r.* FROM runs r JOIN resource_holds h ON r.id=h.run_id "
                    "WHERE h.job_id=?",
                    (parent_id,),
                ).fetchone()
            )

    def improvement_child(self, job_id):
        with self.connection() as con:
            return decode(
                con.execute(
                    "SELECT * FROM jobs WHERE json_extract(spec,'$.parent_job_id')=?", (job_id,)
                ).fetchone()
            )

    def release_expired_holds(self):
        with self.connection(write=True) as con:
            for row in con.execute(
                "SELECT h.job_id FROM resource_holds h JOIN jobs j ON j.id=h.job_id "
                "WHERE h.expires_at<? OR j.completion_mode='finish' OR j.archive_status='failed' "
                "OR (j.archive_status='completed' AND NOT EXISTS "
                "(SELECT 1 FROM jobs c WHERE json_extract(c.spec,'$.parent_job_id')=j.id "
                "AND c.status IN ('pending','estimating','queued','starting'))) ",
                (time.time(),),
            ).fetchall():
                self._release_hold(con, row["job_id"])

    def set_completion_mode(self, job_id, mode, max_improvement_rounds=None):
        values = {"completion_mode": mode}
        if max_improvement_rounds is not None:
            values["max_improvement_rounds"] = max_improvement_rounds
        return self.update_job(job_id, JobUpdate.model_validate(values))

    def update_job(self, job_id, update):
        update = JobUpdate.model_validate(update)
        changes = update.model_dump(exclude_unset=True)
        if not changes:
            raise ValueError("Provide at least one job setting to update")
        with self.connection(write=True) as con:
            job = decode(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job:
                raise ValueError("Unknown job")
            preparation = {"command", "resources", "context_files", "parameters"} & changes.keys()
            if preparation and (job["status"] not in ("pending", "queued", "needs_review")):
                raise ValueError("Command/resources can only change before estimation or launch")
            if job["archive_status"] == "running" and (
                {"intent", "artifact_files", "baseline_job_id"} & changes.keys()
            ):
                raise ValueError("Archive evidence cannot change during an active archive")
            values = {**job["spec"], "completion_mode": job["completion_mode"]}
            for field, value in changes.items():
                values[field] = (
                    {**values.get(field, {}), **value}
                    if field in ("agent_models", "agent_efforts")
                    else value
                )
            spec = JobSpec.model_validate(values)
            policy_changed = bool({"completion_mode", "max_improvement_rounds"} & changes.keys())
            if spec.completion_mode == "improve" and policy_changed:
                if (
                    job["cancel_requested"]
                    or spec.improvement_round >= spec.max_improvement_rounds
                    or (
                        job["status"] in TERMINAL
                        and (
                            job["status"] not in ("succeeded", "failed")
                            or job["archive_status"] != "pending"
                        )
                    )
                ):
                    raise ValueError(
                        "Improvement requires a source job with rounds left, before archive starts"
                    )
                last_archive = con.execute(
                    "SELECT * FROM agent_runs WHERE job_id=? AND phase='archive' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if (
                    last_archive
                    and last_archive["request_sent"]
                    and not con.execute(
                        "SELECT 1 FROM events WHERE job_id=? AND kind='agent_retry_requested' "
                        "AND json_extract(data,'$.phase')='archive' AND at>?",
                        (job_id, last_archive["created_at"]),
                    ).fetchone()
                ):
                    raise ValueError(
                        "Archive callback already sent; "
                        "retry archive explicitly to change its policy"
                    )
            if "max_improvement_rounds" in changes:
                child = con.execute(
                    "SELECT id FROM jobs WHERE json_extract(spec,'$.parent_job_id')=?", (job_id,)
                ).fetchone()
                if child:
                    raise ValueError(
                        f"Adjust the existing improvement child instead: {child['id']}"
                    )
            if spec.baseline_job_id:
                baseline = con.execute(
                    "SELECT server,status,run_id FROM jobs WHERE id=?", (spec.baseline_job_id,)
                ).fetchone()
                if (
                    not baseline
                    or spec.baseline_job_id == job_id
                    or baseline["server"] != job["server"]
                    or baseline["status"] not in ("succeeded", "failed")
                    or not baseline["run_id"]
                ):
                    raise ValueError("Baseline must be an executed experiment on the same server")
            con.execute(
                "UPDATE jobs SET spec=?,completion_mode=?,updated_at=? WHERE id=?",
                (
                    json_text(
                        {key: value for key, value in spec.model_dump().items() if key in values}
                    ),
                    spec.completion_mode,
                    time.time(),
                    job_id,
                ),
            )
            if preparation:
                # Invalidate estimates before the next admission, never mutate a reserved run.
                con.execute(
                    "UPDATE jobs SET status=?,resources=?,estimate=NULL,reason=NULL WHERE id=?",
                    (
                        "queued" if spec.skip_estimate else "pending",
                        json_text(spec.resources.model_dump()) if spec.resources else None,
                        job_id,
                    ),
                )
            if changes.get("completion_mode") == "finish":
                self._end_improvements(con, job_id)
            if policy_changed:
                self.event(
                    con,
                    job_id,
                    "completion_mode_changed",
                    {
                        "mode": spec.completion_mode,
                        "max_improvement_rounds": spec.max_improvement_rounds,
                    },
                )
            # Keep the original spec_hash for submission idempotency; this is an audited control.
            self.event(con, job_id, "job_updated", {"changes": changes})
        return self.job(job_id)

    def _end_improvements(self, con, job_id, *, cancel_running=False):
        ids = [job_id]
        while ids:
            parent = ids.pop()
            self._release_hold(con, parent)
            con.execute(
                "UPDATE jobs SET completion_mode='finish',"
                "spec=json_set(spec,'$.completion_mode','finish'),updated_at=? WHERE id=?",
                (time.time(), parent),
            )
            for row in con.execute(
                "SELECT * FROM jobs WHERE json_extract(spec,'$.parent_job_id')=?", (parent,)
            ).fetchall():
                ids.append(row["id"])
                if cancel_running or row["status"] in PRESTART or row["status"] == "starting":
                    self._cancel(con, row)

    def agent_model(self, agent_id, model):
        with self.connection(write=True) as con:
            con.execute("UPDATE agent_runs SET model=? WHERE id=?", (model, agent_id))

    def agent_effort(self, agent_id, effort):
        with self.connection(write=True) as con:
            con.execute("UPDATE agent_runs SET effort=? WHERE id=?", (effort, agent_id))

    def start_agent(self, job_id, phase, model, *, retry_seconds=0, effort=None):
        if phase not in ("estimate", "archive", "launch"):
            raise ValueError(f"Unknown agent phase: {phase}")
        agent_id = uuid.uuid4().hex
        with self.connection(write=True) as con:
            job = decode(con.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone())
            if not job:
                return None
            source = job["spec"].get("source_thread_id") if phase == "archive" else None
            delivery_id = None
            if phase == "launch":
                if (
                    job["status"] != "starting"
                    or job["launch_status"] != "pending"
                    or job["cancel_requested"]
                ):
                    return None
                con.execute("UPDATE jobs SET launch_status='running' WHERE id=?", (job_id,))
            elif phase == "estimate":
                if job["status"] != "pending" or job["paused"] or job["batch_paused"]:
                    return None
                con.execute(
                    "UPDATE jobs SET status='estimating',updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
            else:
                if job["status"] not in TERMINAL or job["archive_status"] != "pending":
                    return None
                if source:
                    if con.execute(
                        "SELECT 1 FROM agent_runs WHERE phase='archive' AND status='running' "
                        "AND thread_id=?",
                        (source,),
                    ).fetchone():
                        return None
                    if (
                        retry_seconds
                        and con.execute(
                            "SELECT 1 FROM agent_runs WHERE thread_id=? AND status='deferred' "
                            "AND finished_at>?",
                            (source, time.time() - retry_seconds),
                        ).fetchone()
                    ):
                        return None
                    last = con.execute(
                        "SELECT * FROM agent_runs WHERE job_id=? AND phase='archive' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (job_id,),
                    ).fetchone()
                    manual_retry = (
                        last
                        and con.execute(
                            "SELECT 1 FROM events WHERE job_id=? AND kind='agent_retry_requested' "
                            "AND json_extract(data, '$.phase')='archive' AND at>?",
                            (job_id, last["created_at"]),
                        ).fetchone()
                    )
                    delivery_id = (
                        last["delivery_id"]
                        if last and last["status"] != "completed" and not manual_retry
                        else None
                    ) or uuid.uuid4().hex
                    if last and last["delivery_id"] == delivery_id:
                        model = last["model"]
                        effort = last["effort"]
                con.execute(
                    "UPDATE jobs SET archive_status='running',updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
            con.execute(
                "INSERT INTO agent_runs"
                "(id,job_id,phase,status,model,created_at,thread_id,delivery_id,effort,run_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    agent_id,
                    job_id,
                    phase,
                    "running",
                    model,
                    time.time(),
                    source,
                    delivery_id,
                    effort,
                    job["run_id"] if phase == "launch" else None,
                ),
            )
            self.event(con, job_id, "agent_started", {"agent_id": agent_id, "phase": phase})
        return agent_id

    def agent_link(self, agent_id, thread_id, link):
        with self.connection(write=True) as con:
            con.execute(
                "UPDATE agent_runs SET thread_id=?,deep_link=? WHERE id=?",
                (thread_id, link, agent_id),
            )

    def agent_turn(self, agent_id, turn_id):
        with self.connection(write=True) as con:
            con.execute("UPDATE agent_runs SET turn_id=? WHERE id=?", (turn_id, agent_id))

    def agent_delivery(self, agent_id):
        with self.connection() as con:
            agent = decode(
                con.execute("SELECT * FROM agent_runs WHERE id=?", (agent_id,)).fetchone()
            )
            agent["previously_sent"] = bool(
                con.execute(
                    "SELECT 1 FROM agent_runs WHERE delivery_id=? AND request_sent=1",
                    (agent["delivery_id"],),
                ).fetchone()
            )
            return agent

    def agent_dispatched(self, agent_id):
        with self.connection(write=True) as con:
            con.execute("UPDATE agent_runs SET request_sent=1 WHERE id=?", (agent_id,))

    def defer_agent(self, agent_id, reason):
        with self.connection(write=True) as con:
            agent = con.execute("SELECT * FROM agent_runs WHERE id=?", (agent_id,)).fetchone()
            if agent["phase"] != "archive" or agent["status"] != "running":
                raise ValueError("Only an active archive can be deferred")
            con.execute(
                "UPDATE agent_runs SET status='deferred',error=?,finished_at=? WHERE id=?",
                (reason, time.time(), agent_id),
            )
            con.execute(
                "UPDATE jobs SET archive_status='pending',updated_at=? WHERE id=?",
                (time.time(), agent["job_id"]),
            )
            self.event(
                con, agent["job_id"], "agent_deferred", {"agent_id": agent_id, "reason": reason}
            )

    def complete_agent(self, agent_id, response, status=None, resources=None):
        with self.connection(write=True) as con:
            agent = con.execute("SELECT * FROM agent_runs WHERE id=?", (agent_id,)).fetchone()
            if not agent or agent["status"] != "running":
                return
            con.execute(
                "UPDATE agent_runs SET status='completed',response=?,finished_at=? WHERE id=?",
                (json_text(response), time.time(), agent_id),
            )
            if agent["phase"] == "launch":
                job = con.execute("SELECT * FROM jobs WHERE id=?", (agent["job_id"],)).fetchone()
                if (
                    job["status"] != "starting"
                    or job["cancel_requested"]
                    or (agent["run_id"] and agent["run_id"] != job["run_id"])
                ):
                    return
                con.execute(
                    "UPDATE runs SET launch_plan=? WHERE id=(SELECT run_id FROM jobs WHERE id=?)",
                    (json_text(response), agent["job_id"]),
                )
                con.execute(
                    "UPDATE jobs SET launch_status='completed',updated_at=? "
                    "WHERE id=? AND status='starting'",
                    (time.time(), agent["job_id"]),
                )
            elif agent["phase"] == "estimate":
                con.execute(
                    """UPDATE jobs SET status=?,estimate=?,resources=?,reason=?,updated_at=?
                    WHERE id=? AND status='estimating'""",
                    (
                        status,
                        json_text(response),
                        json_text(resources),
                        response["rationale"] if status == "needs_review" else None,
                        time.time(),
                        agent["job_id"],
                    ),
                )
            else:
                con.execute(
                    "UPDATE jobs SET archive_status='completed',analysis=?,updated_at=? WHERE id=?",
                    (json_text(response), time.time(), agent["job_id"]),
                )
            self.event(con, agent["job_id"], "agent_completed", {"agent_id": agent_id})

    def fail_agent(self, agent_id, error, max_attempts, *, permanent=False):
        with self.connection(write=True) as con:
            agent = con.execute("SELECT * FROM agent_runs WHERE id=?", (agent_id,)).fetchone()
            if not agent or agent["status"] != "running":
                return
            con.execute(
                "UPDATE agent_runs SET status='failed',error=?,finished_at=? WHERE id=?",
                (error, time.time(), agent_id),
            )
            count = con.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE job_id=? AND phase=? AND status='failed' "
                "AND (delivery_id IS ? OR ? IS NULL)",
                (agent["job_id"], agent["phase"], agent["delivery_id"], agent["delivery_id"]),
            ).fetchone()[0]
            if agent["phase"] == "launch":
                job = con.execute("SELECT * FROM jobs WHERE id=?", (agent["job_id"],)).fetchone()
                if (
                    job["status"] != "starting"
                    or job["cancel_requested"]
                    or (agent["run_id"] and agent["run_id"] != job["run_id"])
                ):
                    return
                count = con.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE job_id=? AND phase='launch' "
                    "AND status='failed' AND run_id IS ?",
                    (agent["job_id"], agent["run_id"]),
                ).fetchone()[0]
                exhausted = permanent or count >= max_attempts
                con.execute(
                    "UPDATE jobs SET launch_status=?,reason=?,updated_at=? WHERE id=?",
                    ("failed" if exhausted else "pending", error, time.time(), agent["job_id"]),
                )
                if exhausted:
                    run = con.execute(
                        "SELECT run_id FROM jobs WHERE id=?", (agent["job_id"],)
                    ).fetchone()[0]
                    con.execute("DELETE FROM gpu_leases WHERE run_id=?", (run,))
                    con.execute(
                        "UPDATE runs SET status='failed',finished_at=?,result=? WHERE id=?",
                        (
                            time.time(),
                            json_text({"status": "failed", "error": error, "not_launched": True}),
                            run,
                        ),
                    )
                    con.execute("UPDATE jobs SET status='failed' WHERE id=?", (agent["job_id"],))
            elif agent["phase"] == "estimate":
                job = decode(
                    con.execute("SELECT * FROM jobs WHERE id=?", (agent["job_id"],)).fetchone()
                )
                exhausted = permanent or count >= max_attempts
                fallback = exhausted and bool(job["spec"].get("resources"))
                con.execute(
                    "UPDATE jobs SET status=?,reason=?,updated_at=? "
                    "WHERE id=? AND status='estimating'",
                    (
                        "queued" if fallback else "failed" if exhausted else "pending",
                        "Resource estimate unavailable; using submitted resources. " + error
                        if fallback
                        else error,
                        time.time(),
                        agent["job_id"],
                    ),
                )
            else:
                con.execute(
                    "UPDATE jobs SET archive_status=?,updated_at=? WHERE id=?",
                    (
                        "failed" if permanent or count >= max_attempts else "pending",
                        time.time(),
                        agent["job_id"],
                    ),
                )
            self.event(con, agent["job_id"], "agent_failed", {"agent_id": agent_id, "error": error})

    def launch_error(self, run_id):
        with self.connection() as con:
            row = con.execute(
                "SELECT error FROM agent_runs WHERE run_id=? AND phase='launch' "
                "AND status='failed' ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return row["error"] if row else None

    def last_agent(self, job_id, phase):
        with self.connection() as con:
            return decode(
                con.execute(
                    "SELECT * FROM agent_runs WHERE job_id=? AND phase=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (job_id, phase),
                ).fetchone()
            )

    def agent_candidates(self):
        with self.connection() as con:
            return [
                decode(r)
                for r in con.execute(
                    JOB_SELECT + " WHERE (j.status='pending' AND j.paused=0 "
                    "AND COALESCE(bp.paused,b.paused,0)=0) OR "
                    "(j.status='starting' AND j.launch_status='pending' "
                    "AND j.cancel_requested=0) OR "
                    "(j.status IN ('succeeded','failed','cancelled','blocked') "
                    "AND j.archive_status='pending') "
                    "ORDER BY j.created_at,j.id LIMIT 10000"
                )
            ]

    def recover_agents(self, max_attempts):
        with self.connection() as con:
            agents = [
                dict(r) for r in con.execute("SELECT * FROM agent_runs WHERE status='running'")
            ]
        for agent in agents:
            if agent["delivery_id"]:
                self.defer_agent(
                    agent["id"], "Scheduler restarted; checking source conversation delivery"
                )
            else:
                self.fail_agent(
                    agent["id"], "Scheduler stopped during agent analysis", max_attempts
                )

    def retry_agent(self, job_id, phase):
        with self.connection(write=True) as con:
            job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise ValueError(f"Unknown job: {job_id}")
            if phase in ("estimate", "launch") and job["archive_status"] == "running":
                raise ValueError("Wait for the active archive before retrying preparation")
            if (
                phase == "estimate"
                and (
                    job["status"] == "needs_review"
                    or (job["status"] == "failed" and not job["run_id"])
                )
                and not job["cancel_requested"]
            ):
                con.execute("UPDATE jobs SET status='pending',reason=NULL WHERE id=?", (job_id,))
            elif (
                phase == "launch"
                and job["status"] in ("needs_review", "failed")
                and job["launch_status"] == "failed"
                and not job["cancel_requested"]
            ):
                result = con.execute(
                    "SELECT result FROM runs WHERE id=?", (job["run_id"],)
                ).fetchone()
                if result and not json.loads(result["result"] or "{}").get("not_launched"):
                    raise ValueError(
                        "Only a failed preparation that did not execute can be retried"
                    )
                con.execute(
                    "UPDATE jobs SET status='queued',reason=NULL,run_id=NULL,"
                    "launch_status='pending' WHERE id=?",
                    (job_id,),
                )
            elif (
                phase == "archive"
                and job["status"] in TERMINAL
                and job["archive_status"] != "running"
            ):
                con.execute("UPDATE jobs SET archive_status='pending' WHERE id=?", (job_id,))
            else:
                raise ValueError("Job is not eligible for this agent retry")
            if phase in ("estimate", "launch"):
                enabled = json.loads(job["spec"]).get("archive", True)
                con.execute(
                    "UPDATE jobs SET archive_status=?,analysis=NULL,updated_at=? WHERE id=?",
                    ("pending" if enabled else "skipped", time.time(), job_id),
                )
                # A new preparation/execution must not reuse an earlier archive delivery.
                self.event(con, job_id, "agent_retry_requested", {"phase": "archive"})
            self.event(con, job_id, "agent_retry_requested", {"phase": phase})
