"""Experiment storage, snapshots, result normalisation, best versions, export.

Everything that defines a candidate is stored before it is submitted:
immutable snapshot directory + manifest + aggregate SHA-256.  Submissions point
at the snapshot id, and the scheduler re-verifies the bytes immediately before
sending.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .states import (
    ATTEMPT_FINISHED,
    OUTCOME_COMPILE_ERROR,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_RUNTIME_ERROR,
    OUTCOME_RUNNING,
    OUTCOME_SYSTEM_ERROR,
    OUTCOME_TIMEOUT,
    OUTCOME_UNKNOWN,
)
from .util import (
    canonical_json,
    iso,
    json_dumps,
    json_loads,
    new_id,
    now,
    safe_relpath,
    sha256_bytes,
    sha256_file,
    sha256_text,
    stable_hash_files,
    write_text,
)

MAX_SNAPSHOT_FILES = 256
MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024
PASS_STATUSES = {"pass", "accepted"}
COMPILE_STATUSES = {"compile error", "compile_error", "compilation error"}
RUNTIME_STATUSES = {"runtime error", "runtime_error", "run error"}
TIMEOUT_STATUSES = {"time limit exceeded", "time_limit_exceeded", "timeout", "timed out"}
SYSTEM_STATUSES = {"system error", "system_error", "internal error"}
RUNNING_STATUSES = {"running", "pending", "judging", "queued", "compiling", "in queue"}
FAIL_STATUSES = {"fail", "failed", "wrong answer", "wrong_answer", "wa", "presentation error"}


class SnapshotError(RuntimeError):
    pass


def task_path(settings: Settings, task_id: str) -> Path:
    if not task_id or any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz-_" for ch in task_id.lower()):
        raise ValueError("invalid task id")
    return settings.tasks_dir / task_id


def _snapshot_dir(settings: Settings, rel_dir: str) -> Path:
    root = settings.data_dir.resolve()
    path = (root / rel_dir).resolve()
    if root not in path.parents and path != root:
        raise SnapshotError("snapshot path escapes data directory")
    return path


def create_snapshot(
    db: Database,
    settings: Settings,
    *,
    task_id: str,
    attempt_id: str | None,
    files: dict[str, str],
    kind: str = "candidate",
) -> dict:
    if not files:
        raise SnapshotError("snapshot has no files")
    if len(files) > MAX_SNAPSHOT_FILES:
        raise SnapshotError(f"too many files (>{MAX_SNAPSHOT_FILES})")
    cleaned: dict[str, str] = {}
    total = 0
    for raw_path, content in files.items():
        path = safe_relpath(str(raw_path))
        if not isinstance(content, str):
            raise SnapshotError(f"file content must be text: {path}")
        encoded = content.encode("utf-8")
        total += len(encoded)
        if total > MAX_SNAPSHOT_BYTES:
            raise SnapshotError(f"snapshot too large (>{MAX_SNAPSHOT_BYTES} bytes)")
        cleaned[path] = content
    sha, manifest = stable_hash_files(cleaned)
    snapshot_id = new_id("snap")
    rel_dir = f"tasks/{task_id}/snapshots/{snapshot_id}/files"
    final_dir = settings.data_dir / rel_dir
    parent = final_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".tmp-snap-", dir=str(parent)))
    try:
        for path, content in cleaned.items():
            destination = tmp_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content.encode("utf-8"))
        meta = {
            "snapshot_id": snapshot_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "kind": kind,
            "sha256": sha,
            "files": manifest,
            "created_at": now(),
        }
        (tmp_dir / "manifest.json").write_text(json_dumps(meta, pretty=True), encoding="utf-8")
        if final_dir.exists():  # pragma: no cover - id collision
            raise SnapshotError("snapshot id collision")
        os.replace(tmp_dir, final_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    db.execute(
        "INSERT INTO snapshots(id, task_id, attempt_id, kind, sha256, rel_dir, file_count, "
        "total_bytes, files_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            task_id,
            attempt_id,
            kind,
            sha,
            rel_dir,
            len(manifest),
            total,
            canonical_json(manifest),
            now(),
        ),
    )
    row = db.query_one("SELECT * FROM snapshots WHERE id=?", (snapshot_id,))
    assert row is not None
    return row


def read_snapshot(db: Database, settings: Settings, snapshot_id: str) -> dict:
    row = db.query_one("SELECT * FROM snapshots WHERE id=?", (snapshot_id,))
    if not row:
        raise SnapshotError("snapshot not found")
    directory = _snapshot_dir(settings, row["rel_dir"])
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise SnapshotError("snapshot manifest missing")
    meta = json_loads(manifest_path.read_text(encoding="utf-8"), {})
    files: dict[str, str] = {}
    for item in meta.get("files", []):
        path = safe_relpath(item["path"])
        target = directory / path
        if not target.is_file():
            raise SnapshotError(f"snapshot file missing: {path}")
        data = target.read_bytes()
        if sha256_bytes(data) != item["sha256"]:
            raise SnapshotError(f"snapshot file hash mismatch: {path}")
        files[path] = data.decode("utf-8")
    recomputed, _ = stable_hash_files(files)
    if recomputed != row["sha256"]:
        raise SnapshotError("snapshot manifest hash mismatch")
    return {"snapshot": row, "files": files, "manifest": meta}


def snapshot_files(db: Database, settings: Settings, snapshot_id: str) -> dict[str, str]:
    return read_snapshot(db, settings, snapshot_id)["files"]


def add_event(db: Database, task_id: str | None, attempt_id: str | None, event_type: str, payload: dict | None = None) -> None:
    db.insert(
        "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
        (task_id, attempt_id, event_type, canonical_json(payload or {}), now()),
    )


def create_attempt(
    db: Database,
    task_id: str,
    *,
    parent_attempt_id: str | None = None,
    experiment_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt_version: str = "v1",
) -> dict:
    with db.transaction() as conn:
        ordinal = int(
            conn.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM attempts WHERE task_id=?", (task_id,)).fetchone()[0]
        )
        attempt_id = new_id("att")
        current = now()
        conn.execute(
            "INSERT INTO attempts(id, task_id, ordinal, parent_attempt_id, experiment_id, status, provider, model, "
            "prompt_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'generated', ?, ?, ?, ?, ?)",
            (
                attempt_id,
                task_id,
                ordinal,
                parent_attempt_id,
                experiment_id,
                provider,
                model,
                prompt_version,
                current,
                current,
            ),
        )
    row = db.query_one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
    assert row is not None
    add_event(
        db,
        task_id,
        attempt_id,
        "attempt_created",
        {"ordinal": ordinal, "parent": parent_attempt_id, "experiment_id": experiment_id},
    )
    return row


ATTEMPT_PATCH_FIELDS = {
    "status",
    "hypothesis",
    "summary",
    "snapshot_id",
    "generator_id",
    "provider",
    "model",
    "result_json",
    "normalized_json",
    "local_validation_json",
    "error_code",
    "error_message",
    "prompt_tokens",
    "completion_tokens",
    "cost_estimate",
    "step_count",
    "finished_at",
    "parent_attempt_id",
}


def update_attempt(db: Database, attempt_id: str, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items() if k in ATTEMPT_PATCH_FIELDS}
    if not updates:
        return
    updates["updated_at"] = now()
    assignments = ", ".join(f"{key}=?" for key in updates)
    db.execute(f"UPDATE attempts SET {assignments} WHERE id=?", (*updates.values(), attempt_id))


def get_attempt(db: Database, attempt_id: str) -> dict | None:
    return db.query_one("SELECT * FROM attempts WHERE id=?", (attempt_id,))


def current_attempt(db: Database, task_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM attempts WHERE task_id=? ORDER BY ordinal DESC LIMIT 1", (task_id,)
    )


def attempt_history(db: Database, task_id: str, limit: int = 50) -> list[dict]:
    return db.query(
        "SELECT * FROM attempts WHERE task_id=? ORDER BY ordinal DESC LIMIT ?", (task_id, limit)
    )


def attach_snapshot(
    db: Database,
    settings: Settings,
    *,
    task_id: str,
    attempt_id: str,
    files: dict[str, str],
) -> dict:
    attempt = get_attempt(db, attempt_id)
    if not attempt or attempt["task_id"] != task_id:
        raise SnapshotError("attempt does not belong to task")
    if attempt.get("snapshot_id"):
        raise SnapshotError("attempt already has a candidate snapshot")
    snapshot = create_snapshot(
        db, settings, task_id=task_id, attempt_id=attempt_id, files=files, kind="candidate"
    )
    update_attempt(db, attempt_id, snapshot_id=snapshot["id"], status="generated")
    add_event(db, task_id, attempt_id, "candidate_snapshot", {"snapshot_id": snapshot["id"], "sha256": snapshot["sha256"]})
    return snapshot


# ---------------------------------------------------------------------------
# Result normalisation
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _status_norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _testcase_status(item: dict) -> str:
    return str(
        item.get("testcase_status")
        or item.get("status")
        or item.get("result")
        or item.get("state")
        or ""
    ).strip()


def normalize_result(raw: Any) -> dict:
    """Normalise a CANNJudge result while preserving the raw payload.

    The platform's own status strings and time values are kept verbatim.  We do
    not assume units for ``time`` and do not invent missing fields.
    """
    if not isinstance(raw, dict):
        return {
            "outcome": OUTCOME_UNKNOWN,
            "final": False,
            "passed": False,
            "all_passed": False,
            "raw_status": None,
            "message": "result is not an object",
            "testcases": [],
            "testcase_total": 0,
            "testcase_passed": 0,
            "score": None,
            "precision_ratio": None,
            "unknown_fields": [],
        }
    status = raw.get("status") or raw.get("state")
    status_text = str(status or "")
    key = _status_norm(status)
    testcases_raw = raw.get("result")
    if not isinstance(testcases_raw, list):
        testcases_raw = raw.get("testcases") if isinstance(raw.get("testcases"), list) else []
    testcases = []
    for index, item in enumerate(testcases_raw):
        if not isinstance(item, dict):
            item = {"value": item}
        case_status = _testcase_status(item)
        case_key = _status_norm(case_status)
        case_passed = case_key in PASS_STATUSES
        testcases.append(
            {
                "index": index,
                "status": case_status,
                "passed": case_passed,
                "precision_ratio": item.get("precision_ratio"),
                "time": item.get("time"),
                "message": item.get("msg") or item.get("message"),
                "raw": item,
            }
        )
    passed_count = sum(1 for case in testcases if case["passed"])
    total = len(testcases)
    any_running = any(_status_norm(case["status"]) in RUNNING_STATUSES for case in testcases)

    if key in RUNNING_STATUSES or any_running:
        outcome = OUTCOME_RUNNING
        final = False
    elif key in PASS_STATUSES:
        if total and passed_count != total:
            outcome = OUTCOME_FAILED
        else:
            outcome = OUTCOME_PASSED
        final = True
    elif key in COMPILE_STATUSES:
        outcome, final = OUTCOME_COMPILE_ERROR, True
    elif key in RUNTIME_STATUSES:
        outcome, final = OUTCOME_RUNTIME_ERROR, True
    elif key in FAIL_STATUSES:
        outcome, final = OUTCOME_FAILED, True
    elif key in TIMEOUT_STATUSES:
        outcome, final = OUTCOME_TIMEOUT, True
    elif key in SYSTEM_STATUSES:
        outcome, final = OUTCOME_SYSTEM_ERROR, True
    elif key:
        # Unknown platform status strings are preserved as unknown rather than
        # being guessed as a wrong answer.
        outcome, final = OUTCOME_UNKNOWN, True
    else:
        outcome, final = OUTCOME_UNKNOWN, False

    all_passed = outcome == OUTCOME_PASSED and (total == 0 or passed_count == total)
    known = {
        "status",
        "state",
        "msg",
        "message",
        "result",
        "testcases",
        "score",
        "precision_ratio",
        "time",
        "memory",
        "_id",
        "id",
        "submission_id",
        "created_at",
        "updated_at",
        "user_id",
        "problem_id",
        "code_template",
    }
    precision = _as_float(raw.get("precision_ratio"))
    if precision is None:
        ratios = [_as_float(case["precision_ratio"]) for case in testcases]
        ratios = [r for r in ratios if r is not None]
        precision = min(ratios) if ratios else None
    message = raw.get("msg") or raw.get("message")
    if not message:
        # CANNJudge sometimes puts the useful failure reason only on each
        # testcase (e.g. "ProfileError: invocation launched no task").  Surface
        # the first one so the Agent and the UI do not lose the feedback.
        for case in testcases:
            if case.get("message"):
                message = case["message"]
                break
    return {
        "outcome": outcome,
        "final": final,
        "passed": all_passed,
        "all_passed": all_passed,
        "raw_status": status_text or None,
        "message": message,
        "testcases": testcases,
        "testcase_total": total,
        "testcase_passed": passed_count,
        "score": _as_float(raw.get("score")),
        "precision_ratio": precision,
        "time_values": [case["time"] for case in testcases if case.get("time") is not None],
        "time_unit": "platform-provided (unit not assumed)",
        "unknown_fields": sorted(k for k in raw if k not in known),
    }


# ---------------------------------------------------------------------------
# Best versions
# ---------------------------------------------------------------------------


def candidate_metric(normalized: dict, config: dict) -> dict | None:
    """Return a comparable metric, or None when the platform gave none.

    ``time`` is only comparable if the task explicitly declares its unit;
    otherwise a time value is treated as non-comparable rather than guessed.
    """
    choice = str(config.get("metric") or "auto").lower()
    if choice == "auto":
        if normalized.get("score") is not None:
            choice = "score"
        elif normalized.get("precision_ratio") is not None:
            choice = "precision_ratio"
        else:
            choice = "none"
    if choice == "score":
        value = normalized.get("score")
        if value is None:
            return None
        return {"metric": "score", "value": float(value), "direction": "higher", "unit": None}
    if choice == "precision_ratio":
        value = normalized.get("precision_ratio")
        if value is None:
            return None
        return {"metric": "precision_ratio", "value": float(value), "direction": "higher", "unit": None}
    if choice == "time":
        unit = config.get("time_unit")
        values = [_as_float(v) for v in normalized.get("time_values", [])]
        values = [v for v in values if v is not None]
        if not unit or not values:
            return None
        return {"metric": "time", "value": max(values), "direction": "lower", "unit": str(unit)}
    return None


def performance_metric(normalized: dict, config: dict) -> dict | None:
    """Return a performance measurement, never a correctness fallback.

    ``performance_metric`` selects ``max_time`` (default) or ``avg_time``.
    Without timing samples the result is *unknown*, not "no improvement".
    """
    values = [_as_float(v) for v in normalized.get("time_values", [])]
    values = [v for v in values if v is not None]
    if not values:
        return None
    target = str(config.get("performance_metric") or "max_time").lower()
    if target in ("avg", "avg_time", "mean", "mean_time"):
        name, value = "avg_time", sum(values) / len(values)
    else:
        name, value = "max_time", max(values)
    return {
        "metric": name,
        "value": value,
        "max_time": max(values),
        "avg_time": sum(values) / len(values),
        "samples": len(values),
        "unit": config.get("time_unit"),
    }


def update_performance_best(
    db: Database,
    task_id: str,
    attempt_id: str,
    normalized: dict,
    config: dict,
) -> dict | None:
    """Update task-level performance best independently of the correctness baseline."""
    if not normalized.get("all_passed"):
        return None
    metric = performance_metric(normalized, config)
    if metric is None:
        return None
    threshold = float(config.get("performance_min_improvement_pct") or 0.0)
    with db.transaction() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return None
        current = json_loads(task["performance_best_json"], None) if task["performance_best_json"] else None
        if current and current.get("metric") == metric["metric"]:
            current_value = _as_float(current.get("value"))
            if current_value is not None and metric["value"] >= current_value * (1 - threshold / 100.0):
                return None
        summary = {
            "attempt_id": attempt_id,
            "metric": metric["metric"],
            "value": metric["value"],
            "max_time": metric["max_time"],
            "avg_time": metric["avg_time"],
            "samples": metric["samples"],
            "unit": metric["unit"],
            "testcase_passed": normalized.get("testcase_passed"),
            "testcase_total": normalized.get("testcase_total"),
            "updated_at": now(),
        }
        conn.execute(
            "UPDATE tasks SET performance_best_attempt_id=?, performance_best_json=?, updated_at=? WHERE id=?",
            (attempt_id, canonical_json(summary), now(), task_id),
        )
        conn.execute(
            "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (task_id, attempt_id, "performance_best_updated", canonical_json(summary), now()),
        )
        return summary


def recompute_performance_best(db: Database, task_id: str, config: dict | None = None) -> dict | None:
    """Recompute task performance best from persisted passing attempts."""
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return None
    config = config if config is not None else json_loads(task.get("config_json"), {})
    best: tuple[float, dict, dict] | None = None
    for attempt in db.query(
        "SELECT id, normalized_json, ordinal FROM attempts WHERE task_id=? AND normalized_json IS NOT NULL",
        (task_id,),
    ):
        normalized = json_loads(attempt["normalized_json"], {})
        if not normalized.get("all_passed"):
            continue
        metric = performance_metric(normalized, config)
        if metric is None:
            continue
        value = metric["value"]
        if best is None or value < best[0]:
            best = (value, metric, normalized)
    if best is None:
        return None
    metric = best[1]
    summary = {
        "attempt_id": None,
        "metric": metric["metric"],
        "value": metric["value"],
        "max_time": metric["max_time"],
        "avg_time": metric["avg_time"],
        "samples": metric["samples"],
        "unit": metric["unit"],
        "updated_at": now(),
    }
    # Locate the winning attempt again; keep the update atomic.
    for attempt in db.query(
        "SELECT id, normalized_json FROM attempts WHERE task_id=? AND normalized_json IS NOT NULL",
        (task_id,),
    ):
        normalized = json_loads(attempt["normalized_json"], {})
        if not normalized.get("all_passed"):
            continue
        candidate = performance_metric(normalized, config)
        if candidate and candidate["metric"] == metric["metric"] and candidate["value"] == metric["value"]:
            summary["attempt_id"] = attempt["id"]
            break
    if not summary["attempt_id"]:
        return None
    db.execute(
        "UPDATE tasks SET performance_best_attempt_id=?, performance_best_json=?, updated_at=? WHERE id=?",
        (summary["attempt_id"], canonical_json(summary), now(), task_id),
    )
    db.insert(
        "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
        (task_id, summary["attempt_id"], "performance_best_recomputed", canonical_json(summary), now()),
    )
    return summary


def _metric_better(candidate: dict, current: dict | None) -> bool:
    if current is None:
        return True
    if candidate["metric"] != current.get("metric"):
        return False
    if candidate["direction"] != current.get("direction"):
        return False
    if candidate["direction"] == "lower":
        return candidate["value"] < current.get("value")
    return candidate["value"] > current.get("value")


def update_remote_best(
    db: Database,
    task_id: str,
    attempt_id: str,
    normalized: dict,
    config: dict,
) -> dict | None:
    """Update the remote best only for a correct, comparable result."""
    if not normalized.get("all_passed"):
        return None
    metric = candidate_metric(normalized, config)
    with db.transaction() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return None
        current_attempt = task["best_attempt_id"]
        current_metric = json_loads(task["best_metric_json"], None)
        current_attempt_row = None
        if current_attempt:
            current_attempt_row = conn.execute("SELECT * FROM attempts WHERE id=?", (current_attempt,)).fetchone()
        candidate_summary = {
            "attempt_id": attempt_id,
            "metric": metric,
            "testcase_total": normalized.get("testcase_total"),
            "testcase_passed": normalized.get("testcase_passed"),
            "score": normalized.get("score"),
            "precision_ratio": normalized.get("precision_ratio"),
            "raw_status": normalized.get("raw_status"),
            "updated_at": now(),
        }
        better = False
        if not current_attempt:
            better = True
        elif metric is not None:
            better = _metric_better(metric, (current_metric or {}).get("metric"))
        else:
            # Without a comparable metric, only replace a best whose visible
            # testcase pass count is strictly worse.
            current_norm = json_loads(current_attempt_row["normalized_json"], {}) if current_attempt_row else {}
            better = int(normalized.get("testcase_passed") or 0) > int(current_norm.get("testcase_passed") or 0)
        if better:
            conn.execute(
                "UPDATE tasks SET best_attempt_id=?, best_metric_json=?, updated_at=? WHERE id=?",
                (attempt_id, canonical_json(candidate_summary), now(), task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (task_id, attempt_id, "best_updated", canonical_json(candidate_summary), now()),
            )
            return candidate_summary
    return None


def update_local_best(db: Database, task_id: str, attempt_id: str | None, validation: dict) -> None:
    """Local validation best is recorded separately from remote best.

    A CPU reference result never becomes a "remote best" and never implies that
    an Ascend C candidate ran.
    """
    summary = {
        "attempt_id": attempt_id,
        "candidate_executed": bool(validation.get("candidate_executed")),
        "status": validation.get("status"),
        "generator_ok": validation.get("generator_ok"),
        "reference_ok": validation.get("reference_ok"),
        "updated_at": now(),
    }
    db.execute(
        "UPDATE tasks SET local_best_json=?, updated_at=? WHERE id=?",
        (canonical_json(summary), now(), task_id),
    )


# ---------------------------------------------------------------------------
# Model usage
# ---------------------------------------------------------------------------


def record_model_call(
    db: Database,
    *,
    task_id: str | None,
    attempt_id: str | None,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_estimate: float | None,
    cost_known: bool,
    duration_ms: int,
    ok: bool,
    error_code: str | None = None,
) -> None:
    db.insert(
        "INSERT INTO model_calls(id, task_id, attempt_id, provider, model, prompt_tokens, "
        "completion_tokens, total_tokens, cost_estimate, cost_known, duration_ms, ok, error_code, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("call"),
            task_id,
            attempt_id,
            provider,
            model,
            int(prompt_tokens),
            int(completion_tokens),
            int(prompt_tokens) + int(completion_tokens),
            cost_estimate,
            1 if cost_known else 0,
            int(duration_ms),
            1 if ok else 0,
            error_code,
            now(),
        ),
    )


def usage_summary(db: Database, task_id: str) -> dict:
    row = db.query_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "SUM(CASE WHEN cost_known=1 THEN cost_estimate ELSE 0 END) AS known_cost, "
        "SUM(CASE WHEN cost_known=0 THEN 1 ELSE 0 END) AS unknown_cost_calls "
        "FROM model_calls WHERE task_id=?",
        (task_id,),
    ) or {}
    return {
        "calls": int(row.get("calls") or 0),
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "known_cost": row.get("known_cost"),
        "unknown_cost_calls": int(row.get("unknown_cost_calls") or 0),
        "cost_complete": int(row.get("unknown_cost_calls") or 0) == 0,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _safe_zip_name(path: str) -> str:
    return safe_relpath(path)


def build_task_archive(
    db: Database,
    settings: Settings,
    task_id: str,
    *,
    include_problem_package: bool = False,
) -> Path:
    """Build a consistent, checksummed task archive.

    A read transaction captures all database rows before files are copied, so
    the manifest is a consistent snapshot of the task at export time.
    """
    with db.transaction(immediate=False) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("task not found")
        attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY ordinal", (task_id,))]
        snapshots = [dict(r) for r in conn.execute("SELECT * FROM snapshots WHERE task_id=?", (task_id,))]
        generators = [dict(r) for r in conn.execute("SELECT * FROM generators WHERE task_id=?", (task_id,))]
        submissions = [dict(r) for r in conn.execute("SELECT * FROM submissions WHERE task_id=?", (task_id,))]
        model_calls = [dict(r) for r in conn.execute("SELECT * FROM model_calls WHERE task_id=?", (task_id,))]
        events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE task_id=?", (task_id,))]
    task_dict = dict(task)
    for key in ("problem_json", "template_json", "config_json", "best_metric_json", "local_best_json"):
        if task_dict.get(key):
            task_dict[key] = json_loads(task_dict[key], task_dict[key])
    for row in attempts:
        for key in ("result_json", "normalized_json", "local_validation_json"):
            if row.get(key):
                row[key] = json_loads(row[key], row[key])
    for row in snapshots:
        row["files_json"] = json_loads(row["files_json"], [])
    for row in generators:
        for key in ("spec_json", "validation_json"):
            if row.get(key):
                row[key] = json_loads(row[key], row[key])
    for row in submissions:
        if row.get("response_json"):
            row["response_json"] = json_loads(row["response_json"], row["response_json"])
    for row in events:
        row["payload_json"] = json_loads(row["payload_json"], {})

    manifest = {
        "schema": "ascendforge-task-archive/1",
        "product": "AscendForge",
        "generated_at": iso(now()),
        "generated_at_epoch": now(),
        "task": task_dict,
        "attempts": attempts,
        "snapshots": snapshots,
        "generators": generators,
        "submissions": submissions,
        "model_calls": model_calls,
        "events": events,
        "included": {
            "source_snapshots": True,
            "generator_snapshots": True,
            "problem_metadata": True,
            "problem_package": include_problem_package,
            "actual_generated_data_bytes": False,
        },
        "excluded": [
            "model API keys",
            "CANNJudge cookies/session",
            "AscendForge login sessions",
            "server logs",
        ],
        "notes": [
            "Raw platform responses are preserved inside submissions[].response_json.",
            "Snapshot SHA-256 is over canonical path\\0sha256\\0size lines; per-file hashes are in snapshots[].files_json.",
            "No time unit is invented for platform time values.",
        ],
    }

    stamp = int(now())
    target = settings.tmp_dir / f"{task_id}-archive-{stamp}.zip"
    checksums: list[dict] = []

    def add_bytes(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
        name = _safe_zip_name(name)
        zf.writestr(name, data)
        checksums.append({"path": name, "sha256": sha256_bytes(data), "bytes": len(data)})

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        add_bytes(zf, "manifest.json", json_dumps(manifest, pretty=True).encode("utf-8"))
        add_bytes(
            zf,
            "README.txt",
            (
                "AscendForge task archive\n"
                "========================\n"
                "This archive is an additional backup of one optimization task.\n"
                "It excludes credentials, login sessions and server logs.\n"
                "Verify file hashes with SHA256SUMS; see manifest.json for run history.\n"
            ).encode("utf-8"),
        )
        for snapshot in snapshots:
            try:
                bundle = read_snapshot(db, settings, snapshot["id"])
            except SnapshotError:
                add_bytes(zf, f"snapshots/{snapshot['id']}/ERROR.txt", b"snapshot unreadable")
                continue
            for path, content in bundle["files"].items():
                add_bytes(zf, f"snapshots/{snapshot['id']}/files/{path}", content.encode("utf-8"))
            add_bytes(
                zf,
                f"snapshots/{snapshot['id']}/manifest.json",
                json_dumps(bundle["manifest"], pretty=True).encode("utf-8"),
            )
        for generator in generators:
            add_bytes(
                zf,
                f"generators/{generator['id']}.json",
                json_dumps(generator, pretty=True).encode("utf-8"),
            )
        problem_json = task_dict.get("problem_json")
        template_json = task_dict.get("template_json")
        if problem_json:
            add_bytes(zf, "problem/problem.json", json_dumps(problem_json, pretty=True).encode("utf-8"))
        if template_json:
            add_bytes(zf, "problem/template.json", json_dumps(template_json, pretty=True).encode("utf-8"))
        if include_problem_package and task["package_rel"]:
            package = (settings.data_dir / task["package_rel"]).resolve()
            if package.is_file() and settings.data_dir.resolve() in package.parents:
                add_bytes(zf, "problem/package.zip", package.read_bytes())
        for attempt in attempts:
            if attempt.get("result_json"):
                add_bytes(
                    zf,
                    f"results/{attempt['id']}-raw.json",
                    json_dumps(attempt["result_json"], pretty=True).encode("utf-8"),
                )
            if attempt.get("normalized_json"):
                add_bytes(
                    zf,
                    f"results/{attempt['id']}-normalized.json",
                    json_dumps(attempt["normalized_json"], pretty=True).encode("utf-8"),
                )
        sums = "\n".join(f"{item['sha256']}  {item['path']}" for item in sorted(checksums, key=lambda x: x["path"]))
        zf.writestr("SHA256SUMS", sums + "\n")
    return target


def build_snapshot_zip(settings: Settings, snapshot: dict, files: dict[str, str], name: str = "source") -> Path:
    target = settings.tmp_dir / f"{snapshot['id']}-{name}-{int(now())}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(safe_relpath(path), content.encode("utf-8"))
        zf.writestr(
            "SNAPSHOT.json",
            json_dumps(
                {
                    "snapshot_id": snapshot["id"],
                    "sha256": snapshot["sha256"],
                    "file_count": snapshot["file_count"],
                    "note": "Exact UTF-8 bytes submitted for this attempt.",
                },
                pretty=True,
            ),
        )
    return target


def unified_source_diff(old: dict[str, str] | None, new: dict[str, str] | None, *, limit: int = 200_000) -> str:
    old = old or {}
    new = new or {}
    chunks: list[str] = []
    for path in sorted(set(old) | set(new)):
        before = old.get(path)
        after = new.get(path)
        if before == after:
            continue
        diff = difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile=f"a/{path}" if before is not None else "/dev/null",
            tofile=f"b/{path}" if after is not None else "/dev/null",
            n=3,
        )
        chunks.extend(diff)
    text = "".join(chunks)
    if len(text) > limit:
        text = text[:limit] + f"\n... diff truncated at {limit} characters ...\n"
    return text


def archive_integrity(path: Path) -> dict:
    """Verify SHA256SUMS inside an archive without extracting it."""
    problems: list[str] = []
    checked = 0
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "SHA256SUMS" not in names:
            return {"ok": False, "problems": ["missing SHA256SUMS"], "checked": 0}
        lines = zf.read("SHA256SUMS").decode("utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            if name not in names:
                problems.append(f"missing: {name}")
                continue
            actual = hashlib.sha256(zf.read(name)).hexdigest()
            if actual != digest:
                problems.append(f"hash mismatch: {name}")
            checked += 1
    return {"ok": not problems, "problems": problems[:50], "checked": checked}
