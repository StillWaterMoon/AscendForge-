"""Experiment / revision bookkeeping.

An ``experiment`` is a hypothesis with a fixed baseline.  An ``attempt`` is a
revision inside it.  The scheduler, quota and submission machinery stay
unchanged; this module only adds the version lineage that was previously
implicit in ``parent_attempt_id``.

Legacy tasks are migrated lazily: existing attempts are grouped into one
``legacy`` experiment, and the next new revision starts a fresh experiment from
the global best (or the latest snapshot when no passing best exists).
"""

from __future__ import annotations

from typing import Any

from .db import Database
from .experiments import add_event
from .util import json_dumps, json_loads, new_id, now

ACTIVE_STATUSES = ("active", "baseline_building")
TERMINAL_STATUSES = ("completed", "abandoned")


def _task(db: Database, task_id: str) -> dict | None:
    return db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))


def active_experiment(db: Database, task_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM experiments WHERE task_id=? AND status IN (?,?) "
        "ORDER BY ordinal DESC LIMIT 1",
        (task_id, *ACTIVE_STATUSES),
    )


def latest_experiment(db: Database, task_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM experiments WHERE task_id=? ORDER BY ordinal DESC LIMIT 1",
        (task_id,),
    )


def _next_ordinal(db: Database, task_id: str) -> int:
    return int(
        db.scalar("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM experiments WHERE task_id=?", (task_id,))
        or 1
    )


def _best_snapshot(db: Database, task_id: str, preferred: str | None = None) -> str | None:
    if preferred:
        row = db.query_one("SELECT id FROM attempts WHERE id=? AND task_id=? AND snapshot_id IS NOT NULL", (preferred, task_id))
        if row:
            return row["id"]
    row = db.query_one(
        "SELECT id FROM attempts WHERE task_id=? AND snapshot_id IS NOT NULL "
        "ORDER BY ordinal DESC LIMIT 1",
        (task_id,),
    )
    return row["id"] if row else None


def ensure_legacy(db: Database, task_id: str) -> dict | None:
    """Group pre-experiment attempts once, without inventing a hypothesis."""
    if db.query_one("SELECT id FROM experiments WHERE task_id=? LIMIT 1", (task_id,)):
        return None
    attempts = db.query(
        "SELECT id, ordinal, snapshot_id FROM attempts WHERE task_id=? ORDER BY ordinal", (task_id,)
    )
    if not attempts:
        return None
    task = _task(db, task_id)
    if not task:
        return None
    best = task.get("best_attempt_id")
    baseline = best or _best_snapshot(db, task_id)
    working = _best_snapshot(db, task_id)
    current = now()
    experiment_id = new_id("exp")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO experiments(id, task_id, ordinal, status, hypothesis, plan_json, params_json, "
            "baseline_attempt_id, working_attempt_id, experiment_best_id, started_at, finished_at, created_at, updated_at) "
            "VALUES(?, ?, ?, 'legacy', ?, '{}', '{}', ?, ?, ?, ?, NULL, ?, ?)",
            (
                experiment_id,
                task_id,
                1,
                "Migrated pre-experiment attempt history; grouping is legacy and not a verified hypothesis.",
                baseline,
                working,
                best,
                task.get("started_at") or current,
                current,
                current,
            ),
        )
        conn.execute(
            "UPDATE attempts SET experiment_id=? WHERE task_id=? AND experiment_id IS NULL",
            (experiment_id, task_id),
        )
    add_event(
        db,
        task_id,
        None,
        "experiment_legacy_migrated",
        {"experiment_id": experiment_id, "attempts": len(attempts), "baseline": baseline},
    )
    return db.query_one("SELECT * FROM experiments WHERE id=?", (experiment_id,))


def ensure_active(db: Database, task_id: str) -> dict:
    """Return the active experiment, creating a compatible one if needed."""
    current = active_experiment(db, task_id)
    if current:
        return current
    task = _task(db, task_id)
    if not task:
        raise KeyError(f"task not found: {task_id}")
    ensure_legacy(db, task_id)
    baseline = task.get("best_attempt_id") or _best_snapshot(db, task_id)
    current_time = now()
    experiment_id = new_id("exp")
    ordinal = _next_ordinal(db, task_id)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO experiments(id, task_id, ordinal, status, hypothesis, plan_json, params_json, "
            "baseline_attempt_id, working_attempt_id, experiment_best_id, started_at, finished_at, created_at, updated_at) "
            "VALUES(?, ?, ?, 'active', '', '{}', '{}', ?, ?, ?, ?, NULL, ?, ?)",
            (
                experiment_id,
                task_id,
                ordinal,
                baseline,
                baseline,
                None,
                current_time,
                current_time,
                current_time,
            ),
        )
    add_event(
        db,
        task_id,
        None,
        "experiment_started",
        {"experiment_id": experiment_id, "baseline_attempt_id": baseline, "ordinal": ordinal},
    )
    return db.query_one("SELECT * FROM experiments WHERE id=?", (experiment_id,))


def attach_attempt(db: Database, experiment_id: str, attempt_id: str) -> None:
    db.execute("UPDATE attempts SET experiment_id=? WHERE id=?", (experiment_id, attempt_id))


def set_working_revision(db: Database, experiment_id: str, attempt_id: str) -> None:
    db.execute(
        "UPDATE experiments SET working_attempt_id=?, updated_at=? WHERE id=?",
        (attempt_id, now(), experiment_id),
    )


def note_result(db: Database, experiment_id: str, attempt_id: str, normalized: dict) -> bool:
    """Update experiment best only from a real, fully passing remote result.

    During performance optimization a passing-but-slower candidate must not
    become the experiment best.  The fixed baseline is the comparator; when no
    comparable timing exists, a passing result is accepted.
    """
    if not normalized.get("all_passed"):
        return False
    experiment = db.query_one("SELECT * FROM experiments WHERE id=?", (experiment_id,))
    if not experiment:
        return False
    baseline = _perf(db, experiment.get("baseline_attempt_id"))
    candidate = _perf(db, attempt_id)
    if baseline and candidate and candidate["max"] >= baseline["max"]:
        return False
    db.execute(
        "UPDATE experiments SET experiment_best_id=?, updated_at=? WHERE id=?",
        (attempt_id, now(), experiment_id),
    )
    return True


def finish_experiment(db: Database, experiment_id: str, *, status: str = "completed", conclusion: dict | None = None) -> None:
    db.execute(
        "UPDATE experiments SET status=?, finished_at=?, conclusion_json=?, updated_at=? WHERE id=?",
        (status, now(), json_dumps(conclusion or {}), now(), experiment_id),
    )


def _perf(db: Database, attempt_id: str | None) -> dict | None:
    if not attempt_id:
        return None
    row = db.query_one("SELECT normalized_json FROM attempts WHERE id=?", (attempt_id,))
    if not row:
        return None
    normalized = json_loads(row.get("normalized_json"), {})
    values = []
    for value in normalized.get("time_values") or []:
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return {
        "attempt_id": attempt_id,
        "samples": len(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "min": min(values),
    }


def current_context(db: Database, task_id: str) -> dict:
    experiment = active_experiment(db, task_id)
    if not experiment:
        experiment = latest_experiment(db, task_id)
    if not experiment:
        return {}
    task = _task(db, task_id)
    return {
        "id": experiment["id"],
        "ordinal": experiment["ordinal"],
        "status": experiment["status"],
        "hypothesis": experiment.get("hypothesis") or "",
        "baseline_attempt_id": experiment.get("baseline_attempt_id"),
        "working_attempt_id": experiment.get("working_attempt_id"),
        "experiment_best_id": experiment.get("experiment_best_id"),
        "started_at": experiment.get("started_at"),
        "finished_at": experiment.get("finished_at"),
        "conclusion": experiment.get("conclusion_json"),
        "baseline_perf": _perf(db, experiment.get("baseline_attempt_id")),
        "experiment_best_perf": _perf(db, experiment.get("experiment_best_id")),
        "global_best_perf": _perf(db, task.get("best_attempt_id") if task else None),
        "performance_best_attempt_id": task.get("performance_best_attempt_id") if task else None,
        "performance_best": json_loads(task.get("performance_best_json"), None)
        if task and task.get("performance_best_json")
        else None,
    }
