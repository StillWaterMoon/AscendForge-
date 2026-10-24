"""Local Codex optimization loop.

This is the first usable CLI-agent loop:

host workspace -> codex exec/edit -> host snapshot -> scheduler submission
-> real result -> session resume -> next revision.

It intentionally does not let Codex submit to CANNJudge or touch the platform
database.  The worker is still the only sender of official submissions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .agent.preflight import preflight_candidate
from .cli_agent.codex import CodexAdapter, CodexRunResult
from .config import Settings
from .db import Database
from .experiment_service import ensure_active
from .experiments import (
    attach_snapshot,
    create_attempt,
    current_attempt,
    get_attempt,
    read_snapshot,
    update_attempt,
)
from .platform.cannjudge import CannjudgeAdapter
from .platform.service import PlatformService
from .scheduler import Scheduler
from .security import CredentialStore, SecretBox, load_master_key
from .tasks import TaskService
from .util import json_dumps, json_loads, new_id, now
from .worker import Worker


class CodexLoopError(RuntimeError):
    pass


def _services(settings: Settings) -> tuple[Database, TaskService, Scheduler, Worker]:
    db = Database(settings.db_path)
    db.init()
    box = SecretBox(load_master_key(settings.key_file, ""))
    credentials = CredentialStore(db, box)
    platform = PlatformService(db, settings, credentials)
    tasks = TaskService(db, settings, platform)
    scheduler = Scheduler(db, settings, platform, tasks)
    worker = Worker(settings)
    return db, tasks, scheduler, worker


def _template_files(task: dict) -> dict[str, str]:
    template = json_loads(task.get("template_json"), {})
    return {item["path"]: item["content"] for item in CannjudgeAdapter._template_files(template)}


def _parent_source(db: Database, settings: Settings, task: dict) -> tuple[str | None, dict[str, str]]:
    experiment = ensure_active(db, task["id"])
    parent = (
        experiment.get("working_attempt_id")
        or experiment.get("baseline_attempt_id")
        or task.get("performance_best_attempt_id")
        or task.get("best_attempt_id")
    )
    attempt = get_attempt(db, parent) if parent else None
    if attempt and attempt.get("snapshot_id"):
        return attempt["id"], read_snapshot(db, settings, attempt["snapshot_id"])["files"]
    return None, _template_files(task)


def _record_run(db: Database, *, task_id: str, attempt_id: str | None, workspace: Path | None, result: CodexRunResult, changed: list[str], started: float) -> str:
    run_id = new_id("clirun")
    db.insert(
        "INSERT INTO cli_runs(id, task_id, attempt_id, backend, session_ref, run_id, workspace, "
        "exit_code, timed_out, usage_json, last_message, changed_files_json, error, started_at, finished_at) "
        "VALUES(?, ?, ?, 'codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            task_id,
            attempt_id,
            result.session_ref,
            result.run_id,
            str(workspace) if workspace else None,
            result.exit_code,
            1 if result.timed_out else 0,
            json_dumps(result.usage),
            result.last_message[-8000:],
            json_dumps(changed),
            result.error,
            started,
            now(),
        ),
    )
    return run_id


def _wait_submission(db: Database, scheduler: Scheduler, worker: Worker, submission_id: str, timeout_sec: float) -> dict:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        worker.process_queued_submissions()
        scheduler.poll_due()
        row = db.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,))
        if row and row["state"] in ("finished", "failed", "unknown"):
            return row
        time.sleep(2)
    raise CodexLoopError(f"submission {submission_id} did not finish within {timeout_sec}s")


def run_codex_loop(
    settings: Settings,
    task_id: str,
    *,
    rounds: int = 3,
    prompt: str = "",
    timeout_sec: float = 900,
    submission_timeout: float = 1200,
    keep_running: bool = False,
) -> dict:
    db, tasks, scheduler, worker = _services(settings)
    task = tasks.get(task_id)
    if task["status"] == "pending":
        tasks.start(task_id)
    elif task["status"] == "paused":
        tasks.resume(task_id)
    elif task["status"] != "running":
        raise CodexLoopError(f"task is {task['status']}; use Web continue/start before Codex loop")
    config = dict(task.get("config") or {})
    config["execution_mode"] = "codex"
    db.execute(
        "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
        (json_dumps(config), now(), task_id),
    )
    adapter = CodexAdapter(timeout_sec=timeout_sec)
    experiment = ensure_active(db, task_id)
    base_parent, base_files = _parent_source(db, settings, task)
    workspace_root = Path(settings.data_dir) / "cli_workspaces" / task_id
    session_ref: str | None = None
    history: list[dict] = []
    try:
        for round_index in range(1, max(1, rounds) + 1):
            current = get_attempt(db, base_parent) if base_parent else None
            parent_id = current["id"] if current and current.get("snapshot_id") else None
            attempt = create_attempt(
                db,
                task_id,
                parent_attempt_id=parent_id,
                experiment_id=experiment["id"],
                provider="codex-cli",
                model="codex",
                prompt_version="codex-workspace-v1",
            )
            parent_files = dict(base_files)
            task_meta = {
                "task_id": task_id,
                "title": task.get("title"),
                "problem_title": task.get("problem_title"),
                "problem_url": task.get("problem_url"),
                "attempt_id": attempt["id"],
                "attempt_ordinal": attempt["ordinal"],
                "baseline_attempt": experiment.get("baseline_attempt_id"),
                "performance_best": task.get("performance_best_attempt_id"),
                "history": history[-4:],
            }
            workspace = adapter.prepare_workspace(
                workspace_root,
                parent_files,
                task_meta=task_meta,
                instructions=(
                    "You are editing an Ascend C operator candidate. "
                    "Only modify source files under workspace/. Keep the task interface and all dtypes correct. "
                    "Do not access credentials, the platform database, or the official judge. "
                    "The host will save and submit the changed snapshot; do not submit yourself.\n"
                ),
            )
            instruction = prompt or (
                "Inspect TASK.json, AGENTS.md and the current source. Make one focused, reviewable optimization "
                "that should improve performance while preserving correctness. Keep every dtype supported by the "
                "task. Modify only files under workspace/. At the end, summarize the actual code changes."
            )
            if history:
                instruction += (
                    "\nPrevious result: "
                    + json.dumps(history[-1], ensure_ascii=False)[:3000]
                    + "\nContinue from the current workspace; repair it if the result was compile/WA."
                )
            if session_ref:
                result = adapter.resume(
                    session_ref,
                    instruction,
                    workspace=workspace,
                    output_file=workspace_root / "last.txt",
                )
                if result.exit_code != 0:
                    session_ref = None
                    result = adapter.start(
                        instruction,
                        workspace=workspace,
                        output_file=workspace_root / "last.txt",
                        sandbox="workspace-write",
                    )
            else:
                result = adapter.start(
                    instruction,
                    workspace=workspace,
                    output_file=workspace_root / "last.txt",
                    sandbox="workspace-write",
                )
            session_ref = result.session_ref or session_ref
            changes = adapter.collect_changes(workspace, parent_files)
            run_started = now()
            _record_run(
                db,
                task_id=task_id,
                attempt_id=attempt["id"],
                workspace=workspace,
                result=result,
                changed=sorted(changes),
                started=run_started,
            )
            if result.timed_out or result.exit_code != 0:
                update_attempt(
                    db,
                    attempt["id"],
                    status="failed",
                    error_code="CODEX_RUN_FAILED",
                    error_message=(result.error or result.stderr or "codex exited non-zero")[:2000],
                    finished_at=now(),
                )
                raise CodexLoopError(f"Codex run failed: {result.error or result.stderr[-500:]}")
            if not changes:
                update_attempt(
                    db,
                    attempt["id"],
                    status="failed",
                    error_code="CODEX_NO_CHANGES",
                    error_message="Codex did not modify any workspace source file",
                    finished_at=now(),
                )
                raise CodexLoopError("Codex made no source changes")

            files = dict(parent_files)
            files.update(changes)
            check = preflight_candidate(files, json_loads(task.get("problem_json"), {}))
            if not check["ok"]:
                update_attempt(
                    db,
                    attempt["id"],
                    status="failed",
                    error_code="CODEX_PREFLIGHT_FAILED",
                    error_message="; ".join(item["message"] for item in check["errors"][:5])[:2000],
                    finished_at=now(),
                )
                history.append(
                    {
                        "attempt": attempt["ordinal"],
                        "kind": "preflight_failed",
                        "errors": check["errors"][:3],
                    }
                )
                base_parent = experiment.get("working_attempt_id") or experiment.get("baseline_attempt_id")
                base_files = parent_files
                continue
            hypothesis = result.last_message.strip()[:4000] or "Codex workspace edit"
            update_attempt(
                db,
                attempt["id"],
                hypothesis=hypothesis,
                status="local_checking",
                changed_files_json=json_dumps(
                    {"files": sorted(changes), "source": "codex-workspace", "preflight": check}
                ),
            )
            snapshot = attach_snapshot(
                db,
                settings,
                task_id=task_id,
                attempt_id=attempt["id"],
                files=files,
            )
            update_attempt(db, attempt["id"], snapshot_id=snapshot["id"], status="generated")
            submission = scheduler.request_submission(task_id, attempt["id"])
            row = _wait_submission(db, scheduler, worker, submission["id"], submission_timeout)
            normalized = json_loads(
                db.query_one("SELECT normalized_json FROM attempts WHERE id=?", (attempt["id"],))["normalized_json"],
                {},
            )
            history.append(
                {
                    "attempt": attempt["ordinal"],
                    "submission": row["state"],
                    "outcome": normalized.get("outcome"),
                    "passed": normalized.get("testcase_passed"),
                    "total": normalized.get("testcase_total"),
                    "message": (normalized.get("message") or "")[:1000],
                    "time_values": normalized.get("time_values") or [],
                }
            )
            base_parent = attempt["id"]
            base_files = read_snapshot(db, settings, snapshot["id"])["files"]
            if normalized.get("outcome") not in ("passed", "failed"):
                # A compile/runtime error is a repair input, not a direction verdict.
                continue
        return {"task_id": task_id, "rounds": rounds, "history": history, "session_ref": session_ref}
    finally:
        if not keep_running:
            try:
                if tasks.get(task_id)["status"] == "running":
                    tasks.pause(task_id, reason="codex_loop_finished")
            except Exception:
                pass
