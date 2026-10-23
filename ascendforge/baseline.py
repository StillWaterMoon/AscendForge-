"""Manual baseline submission for stage-1 verification.

This path deliberately does not use the model.  It submits the editable template
files exactly once, with explicit user confirmation, so the real login/import/
submit/result loop can be verified before a model API is configured.  It still
goes through the normal snapshot and scheduler path.
"""

from __future__ import annotations

import json

from .config import Settings
from .db import Database
from .experiment_service import ensure_active
from .experiments import add_event, attach_snapshot, create_attempt, update_attempt
from .platform.cannjudge import CannjudgeAdapter
from .scheduler import Scheduler
from .states import TASK_PENDING, TASK_RUNNING
from .tasks import TaskError, TaskService
from .util import now


def create_baseline_submission(
    db: Database,
    settings: Settings,
    tasks: TaskService,
    scheduler: Scheduler,
    task_id: str,
    *,
    actor: str = "user",
) -> dict:
    task = tasks.get(task_id)
    if task["status"] not in (TASK_PENDING, TASK_RUNNING):
        raise TaskError(
            "BAD_STATE",
            f"只有待开始/运行中的任务可以提交人工基线，当前为 {task['status']}",
        )
    existing = int(db.scalar("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task_id,)) or 0)
    if existing:
        raise TaskError("BASELINE_EXISTS", "该任务已经有尝试记录，人工基线只允许一次")
    problem = task.get("problem") or {}
    template = task.get("template") or {}
    files = {
        item["path"]: item["content"]
        for item in CannjudgeAdapter._template_files(template)
        if item.get("editable")
    }
    if not files:
        raise TaskError(
            "NO_EDITABLE_TEMPLATE",
            "题目模板没有可编辑文件，无法构造人工基线；请使用自动 Agent 流程",
        )
    # A baseline task is intentionally one-shot: after the first finished
    # submission it completes instead of trying to call a model that may not be
    # configured yet.
    config = dict(task.get("config") or {})
    config.update({"manual_only": True, "max_attempts": 1, "max_submissions_per_task": 1})
    db.execute(
        "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
        (json.dumps(config, ensure_ascii=False, separators=(",", ":")), now(), task_id),
    )
    experiment = ensure_active(db, task_id)
    attempt = create_attempt(
        db,
        task_id,
        experiment_id=experiment["id"],
        provider="manual",
        model="manual-baseline",
        prompt_version="manual-baseline",
    )
    snapshot = attach_snapshot(
        db,
        settings,
        task_id=task_id,
        attempt_id=attempt["id"],
        files=files,
    )
    update_attempt(
        db,
        attempt["id"],
        hypothesis="人工基线：直接提交题目模板的可编辑文件，验证真实提交/结果链路；不使用模型",
    )
    db.execute(
        "UPDATE experiments SET baseline_attempt_id=COALESCE(baseline_attempt_id, ?), "
        "working_attempt_id=?, updated_at=? WHERE id=?",
        (attempt["id"], attempt["id"], now(), experiment["id"]),
    )
    add_event(
        db,
        task_id,
        attempt["id"],
        "baseline_created",
        {
            "snapshot_id": snapshot["id"],
            "sha256": snapshot["sha256"],
            "files": snapshot["file_count"],
            "actor": actor,
            "note": "manual baseline uses editable template files only",
        },
    )
    # Hold the worker lease across start + queue so an Agent step (which may need
    # an unconfigured model) cannot slip in between.
    lease_holder = "web-baseline"
    db.execute(
        "UPDATE tasks SET worker_id=?, worker_lease_until=? WHERE id=?",
        (lease_holder, now() + 60, task_id),
    )
    try:
        if task["status"] == TASK_PENDING:
            tasks.start(task_id)
        submission = scheduler.request_submission(task_id, attempt["id"])
    finally:
        db.execute(
            "UPDATE tasks SET worker_id=NULL, worker_lease_until=NULL WHERE id=? AND worker_id=?",
            (task_id, lease_holder),
        )
    return {
        "attempt": db.query_one("SELECT * FROM attempts WHERE id=?", (attempt["id"],)),
        "snapshot": snapshot,
        "submission": submission,
        "created_at": now(),
    }
