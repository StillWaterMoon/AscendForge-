"""Task lifecycle service and configuration validation."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .experiment_service import active_experiment, finish_experiment
from .experiments import (
    add_event,
    attempt_history,
    current_attempt,
    task_path,
    usage_summary,
)
from .platform.base import PlatformError
from .platform.service import PlatformService
from .states import (
    ATTEMPT_QUEUED,
    ATTEMPT_SUBMISSION_UNKNOWN,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_NEEDS_LOGIN,
    TASK_NEEDS_REVIEW,
    TASK_PAUSED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_STOPPED,
    TASK_TERMINAL,
    assert_task_transition,
)
from .util import canonical_json, json_dumps, json_loads, new_id, now

log = logging.getLogger(__name__)


class TaskError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def public(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


CONFIG_DEFAULTS: dict[str, Any] = {
    "max_submissions_per_task": 30,
    "max_submissions_per_day": 20,
    "min_submit_interval_sec": 300,
    "max_inflight": 1,
    "max_model_calls": 120,
    "max_tokens": 3_000_000,
    "max_cost": None,
    "max_runtime_sec": 6 * 3600,
    "max_stall_rounds": 5,
    "max_attempts": 30,
    "target_score": None,
    "metric": "auto",
    "time_unit": None,
    "poll_interval_sec": 10,
    "agent_max_steps": 10,
    "max_local_seconds": 30,
    "prompt_version": "v1",
    "execution_mode": "api",
    "codex_runner_active": False,
    "codex_runner_pid": None,
    "codex_runner_status": None,
    "codex_runner_log": None,
    "codex_runner_started_at": None,
    "codex_session_ref": None,
    "codex_runner_token": None,
    "manual_only": False,
    "strategy_prompt": "",
}


def normalize_config(raw: dict | None, *, base: dict | None = None) -> dict:
    config = dict(CONFIG_DEFAULTS)
    if base:
        config.update({k: v for k, v in base.items() if k in CONFIG_DEFAULTS})
    raw = raw or {}
    unknown = sorted(set(raw) - set(CONFIG_DEFAULTS))
    if unknown:
        raise TaskError("BAD_CONFIG", f"未知配置项：{', '.join(unknown)}")
    config.update(raw)

    def integer(key: str, low: int, high: int) -> None:
        try:
            value = int(config[key])
        except (TypeError, ValueError):
            raise TaskError("BAD_CONFIG", f"{key} 必须是整数") from None
        if not low <= value <= high:
            raise TaskError("BAD_CONFIG", f"{key} 必须在 {low}..{high} 之间")
        config[key] = value

    def optional_limit(key: str, high: int) -> None:
        value = config[key]
        if value is None or value == "":
            config[key] = 0
            return
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise TaskError("BAD_CONFIG", f"{key} 必须是整数") from None
        if not 0 <= value <= high:
            raise TaskError("BAD_CONFIG", f"{key} 必须在 0..{high} 之间（0 表示不限）")
        config[key] = value

    optional_limit("max_submissions_per_task", 500)
    optional_limit("max_submissions_per_day", 200)
    integer("min_submit_interval_sec", 60, 86400)
    integer("max_inflight", 1, 3)
    optional_limit("max_model_calls", 5000)
    optional_limit("max_tokens", 200_000_000)
    integer("max_runtime_sec", 60, 7 * 86400)
    integer("max_stall_rounds", 1, 100)
    integer("max_attempts", 1, 500)
    integer("poll_interval_sec", 3, 600)
    integer("agent_max_steps", 1, 30)
    integer("max_local_seconds", 5, 600)
    mode = str(config.get("execution_mode") or "api").lower()
    if mode not in ("api", "cli", "codex"):
        raise TaskError("BAD_CONFIG", "execution_mode 必须是 api、cli 或 codex")
    config["execution_mode"] = mode

    if config["max_cost"] is not None:
        try:
            config["max_cost"] = float(config["max_cost"])
        except (TypeError, ValueError):
            raise TaskError("BAD_CONFIG", "max_cost 必须是数字或留空") from None
        if config["max_cost"] < 0:
            raise TaskError("BAD_CONFIG", "max_cost 不能为负")
    if config["target_score"] is not None:
        try:
            config["target_score"] = float(config["target_score"])
        except (TypeError, ValueError):
            raise TaskError("BAD_CONFIG", "target_score 必须是数字或留空") from None
    if config["metric"] not in ("auto", "score", "precision_ratio", "time", "none"):
        raise TaskError("BAD_CONFIG", "metric 必须是 auto/score/precision_ratio/time/none")
    if config["metric"] == "time" and not config.get("time_unit"):
        raise TaskError("BAD_CONFIG", "使用 time 指标时必须明确 time_unit（平台时间单位未被假定）")
    config["prompt_version"] = str(config.get("prompt_version") or "v1")[:32]
    config["manual_only"] = bool(config.get("manual_only", False))
    strategy = config.get("strategy_prompt") or ""
    if not isinstance(strategy, str):
        raise TaskError("BAD_CONFIG", "strategy_prompt 必须是字符串")
    if len(strategy) > 8000:
        raise TaskError("BAD_CONFIG", "strategy_prompt 不能超过 8000 字符")
    config["strategy_prompt"] = strategy
    return config


class TaskService:
    def __init__(self, db: Database, settings: Settings, platform: PlatformService):
        self.db = db
        self.settings = settings
        self.platform = platform

    # -- reads -----------------------------------------------------------
    def get(self, task_id: str) -> dict:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise TaskError("TASK_NOT_FOUND", "任务不存在")
        task = self._decode(task)
        task["messages"] = self.messages(task_id, limit=50)
        return task

    def messages(self, task_id: str, *, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT id, author, role, content, created_at FROM task_messages "
            "WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
            (task_id, max(1, min(200, int(limit)))),
        )
        return list(reversed(rows))

    def add_message(self, task_id: str, content: str, *, author: str = "admin", role: str = "operator") -> dict:
        text = str(content or "").strip()
        if not text:
            raise TaskError("BAD_MESSAGE", "留言不能为空")
        if len(text) > 4000:
            raise TaskError("BAD_MESSAGE", "留言不能超过 4000 字符")
        self.get(task_id)
        message = {
            "id": new_id("msg"),
            "task_id": task_id,
            "author": str(author or "admin")[:80],
            "role": str(role or "operator")[:32],
            "content": text,
            "created_at": now(),
        }
        self.db.insert(
            "INSERT INTO task_messages(id, task_id, author, role, content, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (message["id"], task_id, message["author"], message["role"], text, message["created_at"]),
        )
        add_event(self.db, task_id, None, "operator_message", {"message_id": message["id"], "chars": len(text)})
        return message

    def list(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM tasks ORDER BY created_at DESC")
        return [self._summary(self._decode(row)) for row in rows]

    @staticmethod
    def _decode(task: dict) -> dict:
        for key in ("problem_json", "template_json", "config_json", "best_metric_json", "local_best_json"):
            if task.get(key):
                task[key] = json_loads(task[key], task[key])
        # Convenience aliases used by services that receive the decoded task.
        if "problem_json" in task:
            task["problem"] = task["problem_json"]
        if "template_json" in task:
            task["template"] = task["template_json"]
        if "config_json" in task:
            task["config"] = task["config_json"]
        return task

    def _summary(self, task: dict) -> dict:
        return {
            "id": task["id"],
            "title": task["title"],
            "platform": task["platform"],
            "problem_id": task["problem_id"],
            "problem_title": task["problem_title"],
            "status": task["status"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "started_at": task["started_at"],
            "best_attempt_id": task["best_attempt_id"],
            "best_metric": task.get("best_metric_json"),
            "last_error": task.get("last_error"),
            "last_error_code": task.get("last_error_code"),
            "stop_reason": task.get("stop_reason"),
            "next_action_at": task["next_action_at"],
        }

    def detail(self, task_id: str) -> dict:
        task = self.get(task_id)
        attempts = attempt_history(self.db, task_id, limit=100)
        for attempt in attempts:
            for key in ("result_json", "normalized_json", "local_validation_json"):
                if attempt.get(key):
                    attempt[key] = json_loads(attempt[key], attempt[key])
        submissions = self.db.query(
            "SELECT * FROM submissions WHERE task_id=? ORDER BY created_at DESC LIMIT 100", (task_id,)
        )
        for submission in submissions:
            if submission.get("response_json"):
                submission["response_json"] = json_loads(submission["response_json"], submission["response_json"])
        usable = {
            "id": task["id"],
            "title": task["title"],
            "platform": task["platform"],
            "problem_id": task["problem_id"],
            "problem_url": task["problem_url"],
            "problem_title": task["problem_title"],
            "problem": task.get("problem_json"),
            "template": task.get("template_json"),
            "status": task["status"],
            "config": task.get("config_json"),
            "messages": task.get("messages") or [],
            "best_attempt_id": task["best_attempt_id"],
            "correctness_baseline_attempt_id": task["best_attempt_id"],
            "best_metric": json_loads(task.get("best_metric_json"), None) if task.get("best_metric_json") else None,
            "performance_best_attempt_id": task.get("performance_best_attempt_id"),
            "performance_best": json_loads(task.get("performance_best_json"), None) if task.get("performance_best_json") else None,
            "local_best": task.get("local_best_json"),
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "started_at": task["started_at"],
            "paused_at": task["paused_at"],
            "stopped_at": task["stopped_at"],
            "stop_reason": task["stop_reason"],
            "last_error": task["last_error"],
            "last_error_code": task["last_error_code"],
            "next_action_at": task["next_action_at"],
            "attempts": attempts,
            "submissions": submissions,
            "usage": usage_summary(self.db, task_id),
            "current_attempt": current_attempt(self.db, task_id),
            "counts": {
                "attempts": len(attempts),
                "candidates": int(
                    self.db.scalar(
                        "SELECT COUNT(*) FROM attempts WHERE task_id=? AND snapshot_id IS NOT NULL", (task_id,)
                    )
                    or 0
                ),
                "infrastructure_failures": int(
                    self.db.scalar(
                        "SELECT COUNT(*) FROM attempts WHERE task_id=? AND status='failed' AND snapshot_id IS NULL",
                        (task_id,),
                    )
                    or 0
                ),
                "submissions": len(submissions),
                "events": int(self.db.scalar("SELECT COUNT(*) FROM events WHERE task_id=?", (task_id,)) or 0),
            },
        }
        return usable

    def events(self, task_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (task_id, int(after), min(500, max(1, int(limit)))),
        )
        for row in rows:
            row["payload"] = json_loads(row["payload_json"], {})
            row.pop("payload_json", None)
        return rows

    # -- create ----------------------------------------------------------
    def create(
        self,
        *,
        title: str,
        problem_url: str,
        config: dict | None = None,
        created_by: str | None = None,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise TaskError("BAD_TITLE", "任务标题不能为空")
        if len(title) > 120:
            raise TaskError("BAD_TITLE", "任务标题不能超过 120 个字符")
        problem_url = (problem_url or "").strip()
        if not problem_url:
            raise TaskError("BAD_PROBLEM", "请填写 CANNJudge 题目链接")
        normalized = normalize_config(config)
        # Fail early with a precise "account cannot connect judge" message.
        try:
            self.platform.adapter()
        except PlatformError as exc:
            raise TaskError(
                "ACCOUNT_CANNOT_CONNECT_JUDGE",
                "账号无法连接 judge：请先在“平台连接”中登录 CANNJudge 账号。"
                + (f"（{exc.message}）" if exc.message else ""),
            ) from None
        task_id = new_id("task")
        directory = task_path(self.settings, task_id)
        directory.mkdir(parents=True, exist_ok=False)
        try:
            imported = self.platform.resolve_and_download(problem_url, directory)
        except PlatformError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            if exc.code == "ACCOUNT_CANNOT_CONNECT_JUDGE":
                raise TaskError(
                    "ACCOUNT_CANNOT_CONNECT_JUDGE",
                    "账号无法连接该题目的 judge：" + exc.message,
                    details={"platform_code": exc.code},
                ) from None
            raise TaskError(
                "PROBLEM_IMPORT_FAILED",
                f"导入题目失败：{exc.message}",
                details=exc.public(),
            ) from None
        problem = imported["problem"]
        template = imported["template"]
        package = imported["package"]
        package_path = Path(package["archive"])
        package_rel = str(package_path.relative_to(self.settings.data_dir)).replace("\\", "/")
        problem_title = str(problem.get("name") or problem.get("title") or problem.get("_id"))
        current = now()
        self.db.execute(
            "INSERT INTO tasks(id, title, platform, problem_id, problem_url, problem_title, problem_json, "
            "template_json, package_rel, status, config_json, created_at, updated_at, next_action_at, created_by) "
            "VALUES(?, ?, 'cannjudge', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                str(problem.get("_id")),
                problem_url,
                problem_title,
                canonical_json(problem),
                canonical_json(template),
                package_rel,
                TASK_PENDING,
                canonical_json(normalized),
                current,
                current,
                current,
                created_by,
            ),
        )
        add_event(
            self.db,
            task_id,
            None,
            "task_created",
            {
                "problem_id": problem.get("_id"),
                "problem_title": problem_title,
                "code_template": problem.get("code_template"),
                "package_sha256": package.get("sha256"),
                "files": len(package.get("files") or []),
            },
        )
        add_event(self.db, task_id, None, "problem_imported", {"problem_url": problem_url})
        return self.get(task_id)

    # -- control ---------------------------------------------------------
    def _transition(self, task_id: str, target: str, *, reason: str | None = None) -> dict:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise TaskError("TASK_NOT_FOUND", "任务不存在")
            current = row["status"]
            if current == target:
                return dict(row)
            assert_task_transition(current, target)
            current_ts = now()
            updates: dict[str, Any] = {"status": target, "updated_at": current_ts}
            if target == TASK_RUNNING:
                updates["started_at"] = row["started_at"] or current_ts
                updates["paused_at"] = None
                updates["next_action_at"] = current_ts
                updates["last_error"] = None
                updates["last_error_code"] = None
            if target == TASK_PAUSED:
                updates["paused_at"] = current_ts
            if target in (TASK_COMPLETED, TASK_FAILED, TASK_STOPPED):
                updates["stopped_at"] = current_ts
                if reason:
                    updates["stop_reason"] = reason
            assignments = ", ".join(f"{key}=?" for key in updates)
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*updates.values(), task_id))
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (task_id, None, f"task_{target}", canonical_json({"reason": reason, "from": current}), current_ts),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            result = dict(updated)
        if target in (TASK_COMPLETED, TASK_FAILED, TASK_STOPPED):
            self._close_active_experiment(task_id, target, reason)
        return result

    def _close_active_experiment(self, task_id: str, target: str, reason: str | None) -> None:
        experiment = active_experiment(self.db, task_id)
        if not experiment:
            return
        reason_text = str(reason or "")
        verified = (
            target == TASK_COMPLETED
            and "target_reached" in reason_text
            and bool(experiment.get("experiment_best_id"))
        )
        status = "completed" if verified else "abandoned"
        conclusion = {
            "status": "completed" if verified else "incomplete",
            "verified": verified,
            "task_terminal_status": target,
            "stop_reason": reason,
            "experiment_best_attempt_id": experiment.get("experiment_best_id"),
        }
        finish_experiment(
            self.db,
            experiment["id"],
            status=status,
            conclusion=conclusion,
            stop_reason=reason or status,
        )

    def start(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task["status"] == TASK_RUNNING:
            return task
        if task["status"] != TASK_PENDING:
            raise TaskError("BAD_STATE", f"只有 pending 任务可以开始，当前为 {task['status']}")
        try:
            self.platform.adapter()
        except PlatformError as exc:
            raise TaskError("ACCOUNT_CANNOT_CONNECT_JUDGE", "账号无法连接 judge，请先重新连接 CANNJudge") from None
        self._transition(task_id, TASK_RUNNING, reason="start")
        return self.get(task_id)

    def _terminate_codex_runner(self, task_id: str, *, reason: str) -> None:
        task = self.db.query_one("SELECT config_json FROM tasks WHERE id=?", (task_id,))
        if not task:
            return
        config = json_loads(task.get("config_json"), {})
        pid = config.get("codex_runner_pid")
        if not pid:
            return
        try:
            pid = int(pid)
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            else:
                os.killpg(pid, 15)
        except Exception as exc:
            log.warning("failed to terminate Codex runner %s: %s", pid, exc)
        config.pop("codex_runner_pid", None)
        config.pop("codex_session_ref", None)
        config["codex_runner_active"] = False
        config["codex_runner_status"] = "terminated"
        self.db.execute(
            "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
            (json_dumps(config), now(), task_id),
        )
        add_event(self.db, task_id, None, "codex_runner_terminated", {"pid": pid, "reason": reason})

    def pause(self, task_id: str, reason: str = "user") -> dict:
        task = self.get(task_id)
        if task["status"] == TASK_PAUSED:
            return task
        if task["status"] != TASK_RUNNING:
            raise TaskError("BAD_STATE", f"只有 running 任务可以暂停，当前为 {task['status']}")
        self._terminate_codex_runner(task_id, reason=f"pause:{reason}")
        self._transition(task_id, TASK_PAUSED, reason=reason)
        return self.get(task_id)

    def resume(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task["status"] == TASK_RUNNING:
            return task
        if task["status"] not in (TASK_PAUSED, TASK_NEEDS_LOGIN, TASK_NEEDS_REVIEW):
            raise TaskError("BAD_STATE", f"当前状态不可恢复：{task['status']}")
        unknown = self.db.query_one(
            "SELECT id FROM submissions WHERE task_id=? AND state IN ('unknown','submitting') LIMIT 1",
            (task_id,),
        )
        if unknown:
            raise TaskError(
                "SUBMISSION_UNKNOWN",
                "存在结果未知的提交，必须先对账或人工确认，禁止直接恢复提交",
                details={"submission_id": unknown["id"]},
            )
        try:
            self.platform.adapter()
        except PlatformError:
            self._set_needs_login(task_id, "CANNJudge 登录已失效，恢复任务前请重新连接账号")
            raise TaskError("ACCOUNT_CANNOT_CONNECT_JUDGE", "账号无法连接 judge，请先重新连接 CANNJudge") from None
        self._transition(task_id, TASK_RUNNING, reason="resume")
        return self.get(task_id)

    def stop(self, task_id: str, reason: str = "user") -> dict:
        task = self.get(task_id)
        if task["status"] in TASK_TERMINAL:
            return task
        self._terminate_codex_runner(task_id, reason=f"stop:{reason}")
        self._transition(task_id, TASK_STOPPED, reason=reason)
        # A queued submission has not been sent.  Stop it before it can leave;
        # submissions already accepted remotely are intentionally left alone.
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, attempt_id FROM submissions WHERE task_id=? AND state='queued'", (task_id,)
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE submissions SET state='failed', error_code='TASK_STOPPED', "
                    "error_message='任务已停止，排队提交未发送', updated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
                conn.execute(
                    "UPDATE attempts SET status='failed', error_code='TASK_STOPPED', "
                    "error_message='任务已停止，排队提交未发送', finished_at=?, updated_at=? WHERE id=?",
                    (now(), now(), row["attempt_id"]),
                )
                conn.execute(
                    "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                    (task_id, row["attempt_id"], "submission_cancelled", canonical_json({"reason": reason}), now()),
                )
        return self.get(task_id)

    # -- worker/internal transitions -------------------------------------
    def _set_needs_login(self, task_id: str, message: str) -> None:
        task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task or task["status"] in TASK_TERMINAL or task["status"] == TASK_NEEDS_LOGIN:
            return
        try:
            self._transition(task_id, TASK_NEEDS_LOGIN, reason=message)
        except Exception:
            log.exception("failed to set needs_login for %s", task_id)
        self.db.execute(
            "UPDATE tasks SET last_error_code='ACCOUNT_CANNOT_CONNECT_JUDGE', last_error=? WHERE id=?",
            (message, task_id),
        )

    def mark_needs_login(self, task_id: str, message: str) -> None:
        self._set_needs_login(task_id, message)

    def mark_needs_review(self, task_id: str, message: str, code: str = "SUBMISSION_UNKNOWN") -> None:
        task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task or task["status"] in TASK_TERMINAL:
            return
        if task["status"] != TASK_NEEDS_REVIEW:
            try:
                self._transition(task_id, TASK_NEEDS_REVIEW, reason=message)
            except Exception:
                log.exception("failed to set needs_review for %s", task_id)
        self.db.execute(
            "UPDATE tasks SET last_error_code=?, last_error=? WHERE id=?",
            (code, message, task_id),
        )

    def complete(self, task_id: str, reason: str) -> None:
        task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task or task["status"] in TASK_TERMINAL:
            return
        self._transition(task_id, TASK_COMPLETED, reason=reason)

    def fail(self, task_id: str, reason: str, code: str = "TASK_FAILED") -> None:
        task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task or task["status"] in TASK_TERMINAL:
            return
        try:
            self._transition(task_id, TASK_FAILED, reason=reason)
        finally:
            self.db.execute(
                "UPDATE tasks SET last_error_code=?, last_error=? WHERE id=?", (code, reason, task_id)
            )

    def set_next_action(self, task_id: str, when: float, *, error_code: str | None = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE tasks SET next_action_at=?, updated_at=?, last_error_code=?, last_error=? WHERE id=?",
            (when, now(), error_code, error, task_id),
        )

    def set_last_error(self, task_id: str, code: str, message: str) -> None:
        self.db.execute(
            "UPDATE tasks SET last_error_code=?, last_error=?, updated_at=? WHERE id=?",
            (code, message, now(), task_id),
        )

    def status_counts(self) -> dict:
        rows = self.db.query("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
        return {row["status"]: row["count"] for row in rows}
