"""SQLite persistence layer.

SQLite is deliberate: AscendForge is a single-user, single-server system with a
small number of writers (web process + one worker).  WAL mode plus short
``BEGIN IMMEDIATE`` transactions gives us atomic quota checks and crash-safe
state without an external database service.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    kind TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'cannjudge',
    problem_id TEXT,
    problem_url TEXT,
    problem_title TEXT,
    problem_json TEXT,
    template_json TEXT,
    package_rel TEXT,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    best_attempt_id TEXT,
    best_metric_json TEXT,
    performance_best_attempt_id TEXT,
    performance_best_json TEXT,
    local_best_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    paused_at REAL,
    stopped_at REAL,
    stop_reason TEXT,
    next_action_at REAL NOT NULL,
    last_error_code TEXT,
    last_error TEXT,
    created_by TEXT,
    worker_id TEXT,
    worker_lease_until REAL,
    is_recovery INTEGER NOT NULL DEFAULT 0,
    recovery_job_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_next ON tasks(status, next_action_at);

CREATE TABLE IF NOT EXISTS workload_specs (
    id TEXT PRIMARY KEY,
    spec_key TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    platform TEXT NOT NULL,
    scope TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    problem_version TEXT,
    testset_key TEXT,
    testcase_ids_json TEXT,
    status TEXT NOT NULL DEFAULT 'partial',
    specs_json TEXT NOT NULL,
    evidence_md TEXT,
    source_job_id TEXT,
    source_task_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(spec_key, revision)
);
CREATE INDEX IF NOT EXISTS idx_workload_specs_key ON workload_specs(spec_key, revision DESC);

CREATE TABLE IF NOT EXISTS workload_recovery_jobs (
    id TEXT PRIMARY KEY,
    spec_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    workspace_task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT,
    progress_json TEXT,
    error_code TEXT,
    error_message TEXT,
    workspace_path TEXT,
    log_path TEXT,
    codex_session_ref TEXT,
    codex_runner_pid INTEGER,
    codex_pause_requested INTEGER NOT NULL DEFAULT 0,
    specs_json TEXT,
    evidence_md TEXT,
    import_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_recovery_jobs_key ON workload_recovery_jobs(spec_key, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_jobs_active
    ON workload_recovery_jobs(spec_key)
    WHERE status IN ('pending', 'running', 'paused', 'external_blocked');

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    hypothesis TEXT,
    plan_json TEXT,
    params_json TEXT,
    search_space_json TEXT,
    success_criteria_json TEXT,
    budget_json TEXT,
    stop_reason TEXT,
    baseline_attempt_id TEXT,
    working_attempt_id TEXT,
    experiment_best_id TEXT,
    conclusion_json TEXT,
    started_at REAL,
    finished_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiments_task ON experiments(task_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(task_id, status);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    parent_attempt_id TEXT,
    status TEXT NOT NULL,
    hypothesis TEXT,
    summary TEXT,
    snapshot_id TEXT,
    diagnostic INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT,
    prompt_version TEXT,
    result_json TEXT,
    normalized_json TEXT,
    local_validation_json TEXT,
    error_code TEXT,
    error_message TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_estimate REAL,
    step_count INTEGER NOT NULL DEFAULT 0,
    experiment_id TEXT,
    revision_kind TEXT,
    config_json TEXT,
    base_snapshot_sha TEXT,
    diff_sha TEXT,
    edit_mode TEXT,
    changed_files_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id, ordinal);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    kind TEXT NOT NULL DEFAULT 'candidate',
    sha256 TEXT NOT NULL,
    rel_dir TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    files_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_task ON snapshots(task_id);

CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    snapshot_id TEXT NOT NULL,
    state TEXT NOT NULL,
    diagnostic INTEGER NOT NULL DEFAULT 0,
    request_hash TEXT,
    idempotency_key TEXT,
    sent_at REAL,
    remote_id TEXT,
    http_status INTEGER,
    response_json TEXT,
    error_code TEXT,
    error_message TEXT,
    unknown_reason TEXT,
    unknown_json TEXT,
    reconciled_at REAL,
    poll_count INTEGER NOT NULL DEFAULT 0,
    next_poll_at REAL,
    experiment_id TEXT,
    evaluation_id TEXT,
    eval_key TEXT,
    replicate_of TEXT,
    reused INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_by TEXT,
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_state ON submissions(state, next_poll_at);
CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_submissions_sent ON submissions(sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_remote ON submissions(remote_id)
    WHERE remote_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    experiment_id TEXT,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    kind TEXT NOT NULL,
    config_json TEXT,
    environment_json TEXT,
    result_json TEXT,
    eval_key TEXT,
    reused INTEGER NOT NULL DEFAULT 0,
    replicate_of TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_task ON evaluations(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_experiment ON evaluations(experiment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_key ON evaluations(task_id, eval_key);

CREATE TABLE IF NOT EXISTS cli_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    backend TEXT NOT NULL,
    session_ref TEXT,
    run_id TEXT,
    workspace TEXT,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT,
    last_message TEXT,
    changed_files_json TEXT,
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_cli_runs_task ON cli_runs(task_id, started_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    attempt_id TEXT,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_attempt ON agent_messages(attempt_id, seq);

CREATE TABLE IF NOT EXISTS task_messages (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    author TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'operator',
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_messages_task ON task_messages(task_id, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # -- connection -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def init(self) -> None:
        # The web and worker processes may start simultaneously on a fresh data
        # directory.  WAL negotiation / schema creation can transiently hit
        # "database is locked"; retry with a bounded backoff instead of crashing.
        deadline = time.monotonic() + 30.0
        last: Exception | None = None
        while time.monotonic() < deadline:
            conn = self.connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                self._migrate(conn)
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                return
            except sqlite3.OperationalError as exc:
                last = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.25)
            finally:
                conn.close()
        raise RuntimeError(f"database initialization did not complete: {last}")

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Idempotent additive migrations for existing data directories.

        Keep historical IDs and attempt/submission rows intact.  New columns are
        nullable; the experiment service assigns legacy rows lazily.
        """
        additions = {
            "experiments": {
                "search_space_json": "TEXT",
                "success_criteria_json": "TEXT",
                "budget_json": "TEXT",
                "stop_reason": "TEXT",
            },
            "tasks": {
                "performance_best_attempt_id": "TEXT",
                "performance_best_json": "TEXT",
                "is_recovery": "INTEGER NOT NULL DEFAULT 0",
                "recovery_job_id": "TEXT",
            },
            "attempts": {
                "diagnostic": "INTEGER NOT NULL DEFAULT 0",
                "experiment_id": "TEXT",
                "revision_kind": "TEXT",
                "config_json": "TEXT",
                "base_snapshot_sha": "TEXT",
                "diff_sha": "TEXT",
                "edit_mode": "TEXT",
                "changed_files_json": "TEXT",
            },
            "submissions": {
                "diagnostic": "INTEGER NOT NULL DEFAULT 0",
                "experiment_id": "TEXT",
                "evaluation_id": "TEXT",
                "eval_key": "TEXT",
                "replicate_of": "TEXT",
                "reused": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in additions.items():
            existing = self._columns(conn, table)
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_recovery_job ON tasks(recovery_job_id)")

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -- simple queries ---------------------------------------------------
    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def query_one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount
        finally:
            conn.close()

    def insert(self, sql: str, params: tuple | list = ()) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        conn = self.connect()
        try:
            row = conn.execute(sql, tuple(params)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    # -- meta / settings --------------------------------------------------
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM schema_meta WHERE key=?", (key,))
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def setting_get(self, key: str, default: Any = None) -> Any:
        from .util import json_loads

        row = self.query_one("SELECT value_json FROM app_settings WHERE key=?", (key,))
        return json_loads(row["value_json"], default) if row else default

    def setting_set(self, key: str, value: Any) -> None:
        from .util import canonical_json, now

        self.execute(
            "INSERT INTO app_settings(key, value_json, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, canonical_json(value), now()),
        )

    # -- maintenance ------------------------------------------------------
    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA integrity_check") or "unknown")

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.scalar("SELECT 1")
                return
            except Exception as exc:  # pragma: no cover - startup race
                last = exc
                time.sleep(0.2)
        raise RuntimeError(f"database not ready: {last}")
