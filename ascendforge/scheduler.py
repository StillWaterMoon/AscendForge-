"""Persistent submission scheduling, quotas, budgets and reconciliation.

All tasks for the single CANNJudge account share one persistent queue.  A
submission is reserved under a ``BEGIN IMMEDIATE`` transaction, so two workers
(or a restarted process) cannot both consume the last quota slot.  The minimum
interval is computed from persisted send timestamps, so a restart does not reset
it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import Settings
from .db import Database
from .experiments import (
    add_event,
    attempt_history,
    get_attempt,
    normalize_result,
    read_snapshot,
    update_attempt,
    update_remote_best,
    usage_summary,
)
from .platform.base import PlatformError, SubmissionUnknown
from .platform.service import PlatformService
from .states import (
    ATTEMPT_FAILED,
    ATTEMPT_FINISHED,
    ATTEMPT_JUDGING,
    ATTEMPT_QUEUED,
    ATTEMPT_SUBMISSION_UNKNOWN,
    ATTEMPT_SUBMITTING,
    SUBMISSION_FAILED,
    SUBMISSION_FINISHED,
    SUBMISSION_JUDGING,
    SUBMISSION_QUEUED,
    SUBMISSION_SUBMITTING,
    SUBMISSION_UNKNOWN,
    TASK_NEEDS_REVIEW,
    TASK_RUNNING,
)
from .tasks import TaskError, TaskService
from .util import canonical_json, json_loads, new_id, now, next_day_start, shanghai_day

log = logging.getLogger(__name__)


class Reservation:
    def __init__(self, ok: bool, reason: str = "", retry_at: float | None = None, detail: dict | None = None):
        self.ok = ok
        self.reason = reason
        self.retry_at = retry_at
        self.detail = detail or {}

    def public(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "retry_at": self.retry_at, "detail": self.detail}


class Scheduler:
    def __init__(self, db: Database, settings: Settings, platform: PlatformService, tasks: TaskService):
        self.db = db
        self.settings = settings
        self.platform = platform
        self.tasks = tasks

    # ------------------------------------------------------------------
    # Quota primitives
    # ------------------------------------------------------------------
    @staticmethod
    def _day_start(ts: float | None = None) -> float:
        # Asia/Shanghai has no DST; deriving from the next local midnight is safe.
        end = next_day_start(ts)
        return end - 86400

    def _last_send(self, conn=None) -> float | None:
        sql = "SELECT MAX(sent_at) FROM submissions WHERE sent_at IS NOT NULL"
        if conn is not None:
            row = conn.execute(sql).fetchone()
            return float(row[0]) if row and row[0] is not None else None
        value = self.db.scalar(sql)
        return float(value) if value is not None else None

    def _inflight(self, conn=None) -> int:
        sql = (
            "SELECT COUNT(*) FROM submissions WHERE state IN "
            "('submitting','submitted','judging','unknown')"
        )
        if conn is not None:
            return int(conn.execute(sql).fetchone()[0])
        return int(self.db.scalar(sql) or 0)

    def _unknown_count(self, conn=None, task_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM submissions WHERE state='unknown'"
        params: tuple = ()
        if task_id:
            sql += " AND task_id=?"
            params = (task_id,)
        if conn is not None:
            return int(conn.execute(sql, params).fetchone()[0])
        return int(self.db.scalar(sql, params) or 0)

    def _sent_since(self, since: float, task_id: str | None = None, conn=None) -> int:
        sql = "SELECT COUNT(*) FROM submissions WHERE sent_at IS NOT NULL AND sent_at>=?"
        params: list[Any] = [since]
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if conn is not None:
            return int(conn.execute(sql, tuple(params)).fetchone()[0])
        return int(self.db.scalar(sql, tuple(params)) or 0)

    # ------------------------------------------------------------------
    # Budgets
    # ------------------------------------------------------------------
    def budget_report(self, task: dict) -> dict:
        config = task.get("config_json") or {}
        if isinstance(config, str):
            config = json_loads(config, {})
        usage = usage_summary(self.db, task["id"])
        attempts = int(self.db.scalar("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task["id"],)) or 0)
        finished = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM attempts WHERE task_id=? AND status IN ('finished','failed')",
                (task["id"],),
            )
            or 0
        )
        sent = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM submissions WHERE task_id=? AND sent_at IS NOT NULL", (task["id"],)
            )
            or 0
        )
        runtime = max(0.0, now() - (task.get("started_at") or now()))
        return {
            "attempts": attempts,
            "finished_attempts": finished,
            "submissions_sent": sent,
            "sends_today": self._sent_since(self._day_start(), task["id"]),
            "inflight": self._inflight(),
            "last_send_at": self._last_send(),
            "runtime_sec": runtime,
            "model_calls": usage["calls"],
            "tokens": usage["total_tokens"],
            "known_cost": usage["known_cost"],
            "cost_complete": usage["cost_complete"],
            "unknown_cost_calls": usage["unknown_cost_calls"],
            "limits": {
                "max_attempts": config.get("max_attempts"),
                "max_submissions_per_task": config.get("max_submissions_per_task"),
                "max_submissions_per_day": config.get("max_submissions_per_day"),
                "min_submit_interval_sec": config.get("min_submit_interval_sec"),
                "max_inflight": config.get("max_inflight"),
                "max_model_calls": config.get("max_model_calls"),
                "max_tokens": config.get("max_tokens"),
                "max_cost": config.get("max_cost"),
                "max_runtime_sec": config.get("max_runtime_sec"),
                "max_stall_rounds": config.get("max_stall_rounds"),
                "target_score": config.get("target_score"),
            },
        }

    def model_allowed(self, task: dict) -> Reservation:
        config = task.get("config_json") or {}
        if isinstance(config, str):
            config = json_loads(config, {})
        report = self.budget_report(task)
        if task["status"] != TASK_RUNNING:
            return Reservation(False, f"task_not_running:{task['status']}")
        if report["attempts"] >= int(config.get("max_attempts") or 10**9):
            return Reservation(False, "max_attempts")
        if report["model_calls"] >= int(config.get("max_model_calls") or 10**9):
            return Reservation(False, "max_model_calls")
        if report["tokens"] >= int(config.get("max_tokens") or 10**9):
            return Reservation(False, "max_tokens")
        if report["runtime_sec"] > float(config.get("max_runtime_sec") or 10**9):
            return Reservation(False, "max_runtime")
        max_cost = config.get("max_cost")
        if max_cost is not None:
            if not report["cost_complete"]:
                return Reservation(
                    False,
                    "cost_unknown",
                    detail={"message": "存在未配置价格的模型调用，无法把未知费用当作 0；请配置单价或移除 max_cost"},
                )
            if float(report["known_cost"] or 0) >= float(max_cost):
                return Reservation(False, "max_cost")
        return Reservation(True)

    # ------------------------------------------------------------------
    # Submission reservation
    # ------------------------------------------------------------------
    def request_submission(self, task_id: str, attempt_id: str) -> dict:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise TaskError("TASK_NOT_FOUND", "任务不存在")
        if task["status"] != TASK_RUNNING:
            raise TaskError("BAD_STATE", f"任务当前为 {task['status']}，不能申请提交")
        attempt = get_attempt(self.db, attempt_id)
        if not attempt or attempt["task_id"] != task_id:
            raise TaskError("BAD_ATTEMPT", "attempt 不属于该任务")
        if not attempt.get("snapshot_id"):
            raise TaskError("NO_SNAPSHOT", "候选代码尚未保存快照，不能提交")
        if attempt["status"] not in ("generated", "local_checking"):
            raise TaskError("BAD_ATTEMPT_STATE", f"attempt 当前为 {attempt['status']}，不能提交")
        with self.db.transaction() as conn:
            unknown = conn.execute(
                "SELECT id FROM submissions WHERE state='unknown' LIMIT 1"
            ).fetchone()
            if unknown:
                raise TaskError(
                    "SUBMISSION_UNKNOWN",
                    "账号存在结果未知的提交，调度器已冻结新提交，必须先用对账/人工确认解决",
                    details={"submission_id": unknown[0]},
                )
            existing = conn.execute(
                "SELECT id FROM submissions WHERE attempt_id=? AND state IN "
                "('queued','submitting','submitted','judging','unknown')",
                (attempt_id,),
            ).fetchone()
            if existing:
                raise TaskError("DUPLICATE_SUBMISSION", "该 attempt 已有在途提交")
            submission_id = new_id("sub")
            current = now()
            snapshot_row = conn.execute(
                "SELECT sha256 FROM snapshots WHERE id=?", (attempt["snapshot_id"],)
            ).fetchone()
            conn.execute(
                "INSERT INTO submissions(id, task_id, attempt_id, snapshot_id, state, request_hash, "
                "created_at, updated_at, next_poll_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    submission_id,
                    task_id,
                    attempt_id,
                    attempt["snapshot_id"],
                    SUBMISSION_QUEUED,
                    snapshot_row["sha256"] if snapshot_row else None,
                    current,
                    current,
                    None,
                ),
            )
            conn.execute(
                "UPDATE attempts SET status=?, updated_at=? WHERE id=?",
                (ATTEMPT_QUEUED, current, attempt_id),
            )
            conn.execute(
                "UPDATE tasks SET next_action_at=?, updated_at=? WHERE id=?",
                (current, current, task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    task_id,
                    attempt_id,
                    "submission_queued",
                    canonical_json({"submission_id": submission_id, "snapshot_id": attempt["snapshot_id"]}),
                    current,
                ),
            )
        return self.db.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,)) or {}

    def _check_limits_locked(self, conn, task: dict, config: dict) -> Reservation:
        if task["status"] != TASK_RUNNING:
            return Reservation(False, f"task_not_running:{task['status']}")
        if self._unknown_count(conn) > 0:
            return Reservation(False, "global_submission_unknown")
        inflight = self._inflight(conn)
        if inflight >= int(config.get("max_inflight") or 1):
            return Reservation(False, "max_inflight", detail={"inflight": inflight})
        last = self._last_send(conn)
        interval = int(config.get("min_submit_interval_sec") or 300)
        if last is not None and now() < last + interval:
            return Reservation(False, "min_interval", retry_at=last + interval)
        sent_task = self._sent_since(0, task["id"], conn)
        if sent_task >= int(config.get("max_submissions_per_task") or 10**9):
            return Reservation(False, "max_submissions_per_task")
        day_start = self._day_start()
        sent_day = self._sent_since(day_start, task["id"], conn)
        if sent_day >= int(config.get("max_submissions_per_day") or 10**9):
            return Reservation(False, "max_submissions_per_day", retry_at=next_day_start())
        runtime = now() - (task["started_at"] or now())
        if runtime > float(config.get("max_runtime_sec") or 10**9):
            return Reservation(False, "max_runtime")
        return Reservation(True)

    def claim_submission(self, submission_id: str) -> tuple[dict | None, Reservation]:
        """Atomically move a queued submission to ``submitting`` and count it.

        ``sent_at`` is intentionally set before the HTTP request.  A send that
        later fails or becomes unknown still consumes the frequency quota.
        """
        with self.db.transaction() as conn:
            submission = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if not submission:
                return None, Reservation(False, "not_found")
            if submission["state"] != SUBMISSION_QUEUED:
                return None, Reservation(False, f"not_queued:{submission['state']}")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (submission["task_id"],)).fetchone()
            if not task:
                return None, Reservation(False, "task_not_found")
            config = json_loads(task["config_json"], {})
            check = self._check_limits_locked(conn, dict(task), config)
            if not check.ok:
                return None, check
            current = now()
            conn.execute(
                "UPDATE submissions SET state=?, sent_at=?, updated_at=? WHERE id=?",
                (SUBMISSION_SUBMITTING, current, current, submission_id),
            )
            conn.execute(
                "UPDATE attempts SET status=?, updated_at=? WHERE id=?",
                (ATTEMPT_SUBMITTING, current, submission["attempt_id"]),
            )
            conn.execute(
                "UPDATE tasks SET next_action_at=?, updated_at=? WHERE id=?",
                (current + int(config.get("min_submit_interval_sec") or 300), current, submission["task_id"]),
            )
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    submission["task_id"],
                    submission["attempt_id"],
                    "submission_sending",
                    canonical_json({"submission_id": submission_id}),
                    current,
                ),
            )
            claimed = dict(conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone())
        return claimed, Reservation(True)

    def send_claimed(self, submission_id: str) -> dict:
        submission = self.db.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,))
        if not submission or submission["state"] != SUBMISSION_SUBMITTING:
            raise TaskError("BAD_SUBMISSION", "提交不在 submitting 状态")
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (submission["task_id"],))
        if not task:
            raise TaskError("TASK_NOT_FOUND", "任务不存在")
        attempt = get_attempt(self.db, submission["attempt_id"])
        if not attempt or not attempt.get("snapshot_id"):
            raise TaskError("NO_SNAPSHOT", "attempt 缺少快照")
        try:
            bundle = read_snapshot(self.db, self.settings, attempt["snapshot_id"])
        except Exception as exc:
            self._fail_submission(submission, "SNAPSHOT_INTEGRITY", f"提交前快照校验失败：{exc}")
            raise TaskError("SNAPSHOT_INTEGRITY", f"提交前快照校验失败：{exc}") from None
        problem = json_loads(task["problem_json"], {})
        template = json_loads(task["template_json"], {})
        config = json_loads(task["config_json"], {})
        try:
            result = self.platform.submit(problem, template, bundle["files"])
        except SubmissionUnknown as exc:
            self._mark_unknown(submission, exc.message, exc.public())
            self._schedule_reconcile(submission_id)
            return {"state": SUBMISSION_UNKNOWN, "message": exc.message}
        except PlatformError as exc:
            self._handle_send_error(submission, task, config, exc)
            return {"state": SUBMISSION_FAILED, "error": exc.public()}
        except Exception as exc:  # unexpected adapter failure: ambiguous outcome
            log.exception("unexpected submit failure")
            self._mark_unknown(submission, f"提交时发生未预期错误：{exc}", {"error": str(exc)})
            self._schedule_reconcile(submission_id)
            return {"state": SUBMISSION_UNKNOWN, "message": str(exc)}
        remote_id = result.get("submission_id")
        current = now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE submissions SET state=?, remote_id=?, http_status=?, response_json=?, "
                "error_code=NULL, error_message=NULL, poll_count=0, next_poll_at=?, updated_at=? WHERE id=?",
                (
                    SUBMISSION_JUDGING,
                    remote_id,
                    200,
                    canonical_json(result.get("raw") or {}),
                    current + int(config.get("poll_interval_sec") or 10),
                    current,
                    submission_id,
                ),
            )
            conn.execute(
                "UPDATE attempts SET status=?, updated_at=? WHERE id=?",
                (ATTEMPT_JUDGING, current, submission["attempt_id"]),
            )
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    submission["task_id"],
                    submission["attempt_id"],
                    "submission_accepted",
                    canonical_json({"submission_id": submission_id, "remote_id": remote_id}),
                    current,
                ),
            )
        return {"state": SUBMISSION_JUDGING, "remote_id": remote_id}

    # ------------------------------------------------------------------
    # Error handling / unknown reconciliation
    # ------------------------------------------------------------------
    def _fail_submission(self, submission: dict, code: str, message: str) -> None:
        current = now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE submissions SET state=?, error_code=?, error_message=?, updated_at=? WHERE id=?",
                (SUBMISSION_FAILED, code, message[:2000], current, submission["id"]),
            )
            conn.execute(
                "UPDATE attempts SET status=?, error_code=?, error_message=?, finished_at=?, updated_at=? WHERE id=?",
                (ATTEMPT_FAILED, code, message[:2000], current, current, submission["attempt_id"]),
            )
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    submission["task_id"],
                    submission["attempt_id"],
                    "submission_failed",
                    canonical_json({"submission_id": submission["id"], "code": code, "message": message[:1000]}),
                    current,
                ),
            )

    def _mark_unknown(self, submission: dict, reason: str, detail: dict) -> None:
        current = now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE submissions SET state=?, unknown_reason=?, unknown_json=?, updated_at=?, "
                "next_poll_at=? WHERE id=?",
                (SUBMISSION_UNKNOWN, reason[:2000], canonical_json(detail), current, current + 30, submission["id"]),
            )
            conn.execute(
                "UPDATE attempts SET status=?, error_code='SUBMISSION_UNKNOWN', error_message=?, updated_at=? WHERE id=?",
                (ATTEMPT_SUBMISSION_UNKNOWN, reason[:2000], current, submission["attempt_id"]),
            )
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    submission["task_id"],
                    submission["attempt_id"],
                    "submission_unknown",
                    canonical_json({"submission_id": submission["id"], "reason": reason[:1000]}),
                    current,
                ),
            )
        self.tasks.mark_needs_review(
            submission["task_id"], f"提交结果未知：{reason}。请对账或人工确认后再恢复。"
        )

    def _handle_send_error(self, submission: dict, task: dict, config: dict, exc: PlatformError) -> None:
        self._fail_submission(submission, exc.code, exc.message)
        if exc.code in ("AUTH_EXPIRED", "AUTH_REQUIRED"):
            self.tasks.mark_needs_login(submission["task_id"], "CANNJudge 登录已失效，提交已暂停")
        elif exc.code == "FORBIDDEN":
            self.tasks.mark_needs_review(
                submission["task_id"], "CANNJudge 拒绝提交（权限/题目问题），请检查账号权限", code=exc.code
            )
        elif exc.code == "RATE_LIMITED":
            retry_at = now() + self._retry_after(exc)
            self.tasks.set_next_action(
                submission["task_id"],
                retry_at,
                error_code=exc.code,
                error=f"平台限流，退避到 {int(retry_at)}",
            )
        else:
            self.tasks.set_next_action(
                submission["task_id"],
                now() + 30,
                error_code=exc.code,
                error=exc.message[:500],
            )

    @staticmethod
    def _retry_after(exc: PlatformError) -> int:
        raw = (exc.details or {}).get("retry_after")
        try:
            return max(5, min(3600, int(float(str(raw)))))
        except (TypeError, ValueError):
            pass
        if raw:
            try:
                from email.utils import parsedate_to_datetime

                target = parsedate_to_datetime(str(raw))
                if target is not None:
                    return max(5, min(3600, int(target.timestamp() - now())))
            except (TypeError, ValueError, OverflowError):
                pass
        return 60

    def _schedule_reconcile(self, submission_id: str, delay: float = 5.0) -> None:
        self.db.execute(
            "UPDATE submissions SET next_poll_at=?, updated_at=? WHERE id=?",
            (now() + delay, now(), submission_id),
        )

    def reconcile_submission(self, submission_id: str, *, actor: str = "worker") -> dict:
        submission = self.db.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,))
        if not submission:
            raise TaskError("SUBMISSION_NOT_FOUND", "提交记录不存在")
        if submission["state"] not in (SUBMISSION_UNKNOWN, SUBMISSION_SUBMITTING):
            return {"state": submission["state"], "message": "not unknown"}
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (submission["task_id"],))
        if not task:
            raise TaskError("TASK_NOT_FOUND", "任务不存在")
        problem_id = task["problem_id"]
        user_id = self.platform.account_user_id()
        if not user_id:
            raise TaskError("NOT_CONNECTED", "CANNJudge 账号未连接，无法对账")
        if submission.get("remote_id"):
            try:
                raw = self.platform.get_submission(submission["remote_id"])
            except PlatformError as exc:
                if exc.code in ("AUTH_EXPIRED", "AUTH_REQUIRED"):
                    self.tasks.mark_needs_login(submission["task_id"], "对账时登录失效")
                return {"state": SUBMISSION_UNKNOWN, "error": exc.public()}
            normalized = normalize_result(raw)
            if normalized.get("final"):
                self._finish_submission(submission, raw, normalized, actor=actor)
                self._resume_if_reconciled(submission["task_id"])
                return {"state": SUBMISSION_FINISHED, "outcome": normalized.get("outcome")}
            self.db.execute(
                "UPDATE submissions SET state=?, response_json=?, next_poll_at=?, updated_at=? WHERE id=?",
                (SUBMISSION_JUDGING, canonical_json(raw), now() + 20, now(), submission_id),
            )
            update_attempt(self.db, submission["attempt_id"], status=ATTEMPT_JUDGING, updated_at=now())
            return {"state": SUBMISSION_JUDGING}
        try:
            candidates = self.platform.find_recent(problem_id, user_id, (submission.get("sent_at") or now()) - 600)
        except PlatformError as exc:
            if exc.code in ("AUTH_EXPIRED", "AUTH_REQUIRED"):
                self.tasks.mark_needs_login(submission["task_id"], "对账时登录失效")
            return {"state": SUBMISSION_UNKNOWN, "error": exc.public()}
        known = {
            row["remote_id"]
            for row in self.db.query("SELECT remote_id FROM submissions WHERE remote_id IS NOT NULL")
        }
        candidates = [
            c
            for c in candidates
            if str(c.get("_id") or c.get("id") or c.get("submission_id") or "") not in known
            and (c.get("_id") or c.get("id") or c.get("submission_id"))
        ]
        if len(candidates) == 1:
            remote_id = str(candidates[0].get("_id") or candidates[0].get("id") or candidates[0].get("submission_id"))
            self.db.execute(
                "UPDATE submissions SET remote_id=?, reconciled_at=?, updated_at=? WHERE id=?",
                (remote_id, now(), now(), submission_id),
            )
            add_event(
                self.db,
                submission["task_id"],
                submission["attempt_id"],
                "submission_reconciled",
                {"submission_id": submission_id, "remote_id": remote_id, "actor": actor},
            )
            submission = dict(submission)
            submission["remote_id"] = remote_id
            return self.reconcile_submission(submission_id, actor=actor)
        detail = {"candidates": len(candidates)}
        self.db.execute(
            "UPDATE submissions SET response_json=?, updated_at=? WHERE id=?",
            (canonical_json({"reconcile_candidates": candidates[:10]}), now(), submission_id),
        )
        self.tasks.mark_needs_review(
            submission["task_id"],
            "无法自动对账：未找到唯一匹配的远端提交，请人工确认 remote id 或标记失败",
        )
        return {"state": SUBMISSION_UNKNOWN, "detail": detail}

    def _resume_if_reconciled(self, task_id: str) -> None:
        task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if task and task["status"] == TASK_NEEDS_REVIEW and self._unknown_count(task_id=task_id) == 0:
            try:
                self.tasks._transition(task_id, TASK_RUNNING, reason="reconciliation complete")
            except Exception:
                log.exception("could not resume task %s after reconciliation", task_id)

    def resolve_manual(
        self,
        submission_id: str,
        *,
        action: str,
        remote_id: str | None = None,
        note: str = "",
        actor: str = "user",
    ) -> dict:
        submission = self.db.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,))
        if not submission:
            raise TaskError("SUBMISSION_NOT_FOUND", "提交记录不存在")
        if submission["state"] not in (SUBMISSION_UNKNOWN, SUBMISSION_SUBMITTING):
            raise TaskError("BAD_STATE", "只有 unknown/submitting 提交需要人工确认")
        if action == "adopt":
            if not remote_id:
                raise TaskError("BAD_INPUT", "adopt 必须提供远端 submission id")
            self.db.execute(
                "UPDATE submissions SET remote_id=?, reconciled_at=?, resolved_by=?, resolution_note=?, updated_at=? "
                "WHERE id=?",
                (remote_id, now(), actor, note[:1000], now(), submission_id),
            )
            result = self.reconcile_submission(submission_id, actor=actor)
            # A unique/adopted reconciliation unblocks the task.
            task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (submission["task_id"],))
            if task and task["status"] == TASK_NEEDS_REVIEW and self._unknown_count(task_id=submission["task_id"]) == 0:
                try:
                    self.tasks._transition(submission["task_id"], TASK_RUNNING, reason="manual reconciliation")
                except Exception:
                    pass
            return result
        if action == "mark_failed":
            self._fail_submission(submission, "MANUAL_UNKNOWN_RESOLVED", note or "人工确认：远端未受理")
            self.db.execute(
                "UPDATE submissions SET resolved_by=?, resolution_note=?, reconciled_at=? WHERE id=?",
                (actor, note[:1000], now(), submission_id),
            )
            task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (submission["task_id"],))
            if task and task["status"] == TASK_NEEDS_REVIEW and self._unknown_count(task_id=submission["task_id"]) == 0:
                try:
                    self.tasks._transition(submission["task_id"], TASK_RUNNING, reason="manual unknown resolved")
                except Exception:
                    pass
            return {"state": SUBMISSION_FAILED}
        raise TaskError("BAD_INPUT", "action 必须是 adopt 或 mark_failed")

    # ------------------------------------------------------------------
    # Polling results
    # ------------------------------------------------------------------
    def poll_due(self, limit: int = 5) -> int:
        rows = self.db.query(
            "SELECT * FROM submissions WHERE state IN ('submitted','judging') AND next_poll_at IS NOT NULL "
            "AND next_poll_at<=? ORDER BY next_poll_at ASC LIMIT ?",
            (now(), max(1, limit)),
        )
        processed = 0
        for submission in rows:
            processed += 1
            try:
                raw = self.platform.get_submission(submission["remote_id"])
            except PlatformError as exc:
                if exc.code in ("AUTH_EXPIRED", "AUTH_REQUIRED"):
                    self.tasks.mark_needs_login(submission["task_id"], "查询评测结果时登录失效")
                    self._reschedule_poll(submission, 60)
                elif exc.retryable:
                    self._reschedule_poll(submission, 30)
                else:
                    self._mark_unknown(submission, f"查询结果失败：{exc.message}", exc.public())
                continue
            except Exception:
                log.exception("unexpected poll failure")
                self._reschedule_poll(submission, 30)
                continue
            normalized = normalize_result(raw)
            if normalized.get("final"):
                self._finish_submission(submission, raw, normalized, actor="worker")
            else:
                self._reschedule_poll(submission, self._poll_interval(submission["task_id"]), raw=raw)
        return processed

    def _poll_interval(self, task_id: str) -> int:
        task = self.db.query_one("SELECT config_json FROM tasks WHERE id=?", (task_id,))
        config = json_loads(task["config_json"], {}) if task else {}
        return int(config.get("poll_interval_sec") or 10)

    def _reschedule_poll(self, submission: dict, delay: float, *, raw: dict | None = None) -> None:
        current = now()
        if raw is not None:
            self.db.execute(
                "UPDATE submissions SET response_json=?, poll_count=poll_count+1, next_poll_at=?, updated_at=? WHERE id=?",
                (canonical_json(raw), current + delay, current, submission["id"]),
            )
        else:
            self.db.execute(
                "UPDATE submissions SET poll_count=poll_count+1, next_poll_at=?, updated_at=? WHERE id=?",
                (current + delay, current, submission["id"]),
            )

    def _finish_submission(self, submission: dict, raw: dict, normalized: dict, *, actor: str) -> None:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (submission["task_id"],))
        if not task:
            return
        config = json_loads(task["config_json"], {})
        current = now()
        outcome = normalized.get("outcome")
        error_code = None
        if outcome in ("failed", "compile_error", "runtime_error", "timeout", "system_error"):
            error_code = outcome.upper()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE submissions SET state=?, response_json=?, error_code=?, updated_at=?, "
                "next_poll_at=NULL, poll_count=poll_count+1 WHERE id=?",
                (SUBMISSION_FINISHED, canonical_json(raw), error_code, current, submission["id"]),
            )
            conn.execute(
                "UPDATE attempts SET status=?, result_json=?, normalized_json=?, error_code=?, error_message=?, "
                "finished_at=?, updated_at=? WHERE id=?",
                (
                    ATTEMPT_FINISHED,
                    canonical_json(raw),
                    canonical_json(normalized),
                    error_code,
                    normalized.get("message"),
                    current,
                    current,
                    submission["attempt_id"],
                ),
            )
            conn.execute(
                "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    submission["task_id"],
                    submission["attempt_id"],
                    "submission_finished",
                    canonical_json(
                        {
                            "submission_id": submission["id"],
                            "remote_id": submission.get("remote_id"),
                            "outcome": outcome,
                            "raw_status": normalized.get("raw_status"),
                            "testcase_passed": normalized.get("testcase_passed"),
                            "testcase_total": normalized.get("testcase_total"),
                            "actor": actor,
                        }
                    ),
                    current,
                ),
            )
        if normalized.get("all_passed"):
            update_remote_best(self.db, submission["task_id"], submission["attempt_id"], normalized, config)
        self.tasks.set_next_action(submission["task_id"], current)
        self._evaluate_stop(submission["task_id"])

    # ------------------------------------------------------------------
    # Stop conditions
    # ------------------------------------------------------------------
    def _stall_rounds(self, task: dict) -> int:
        best_id = task.get("best_attempt_id")
        if not best_id:
            return int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM attempts WHERE task_id=? AND status IN ('finished','failed')",
                    (task["id"],),
                )
                or 0
            )
        best = get_attempt(self.db, best_id)
        if not best:
            return 0
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM attempts WHERE task_id=? AND ordinal>? AND status IN ('finished','failed')",
                (task["id"], best["ordinal"]),
            )
            or 0
        )

    def _evaluate_stop(self, task_id: str) -> str | None:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or task["status"] != TASK_RUNNING:
            return None
        config = json_loads(task["config_json"], {})
        report = self.budget_report(dict(task))
        reason = None
        if config.get("manual_only") and int(
            self.db.scalar(
                "SELECT COUNT(*) FROM submissions WHERE task_id=? AND state='finished'", (task_id,)
            )
            or 0
        ) >= 1:
            reason = "manual_baseline_finished"
        target = config.get("target_score")
        best = json_loads(task.get("best_metric_json"), None)
        if target is not None and best and isinstance(best.get("metric"), dict):
            metric = best["metric"]
            value = metric.get("value")
            if value is not None:
                if metric.get("direction") == "higher" and float(value) >= float(target):
                    reason = f"target_score_reached:{value}"
                elif metric.get("direction") == "lower" and float(value) <= float(target):
                    reason = f"target_score_reached:{value}"
        if reason is None and report["attempts"] >= int(config.get("max_attempts") or 10**9):
            reason = "max_attempts_reached"
        if reason is None and report["submissions_sent"] >= int(config.get("max_submissions_per_task") or 10**9):
            reason = "max_submissions_reached"
        if reason is None and report["runtime_sec"] > float(config.get("max_runtime_sec") or 10**9):
            reason = "max_runtime_reached"
        if reason is None and report["model_calls"] >= int(config.get("max_model_calls") or 10**9):
            reason = "max_model_calls_reached"
        if reason is None and report["tokens"] >= int(config.get("max_tokens") or 10**9):
            reason = "max_tokens_reached"
        if reason is None and config.get("max_cost") is not None and report["cost_complete"]:
            if float(report["known_cost"] or 0) >= float(config["max_cost"]):
                reason = "max_cost_reached"
        if reason is None and self._stall_rounds(dict(task)) >= int(config.get("max_stall_rounds") or 10**9):
            reason = "stall_limit_reached"
        if reason:
            self.tasks.complete(task_id, reason)
            return reason
        return None

    # ------------------------------------------------------------------
    # Recovery / UI helpers
    # ------------------------------------------------------------------
    def reconcile_due(self, limit: int = 3) -> int:
        """Try to reconcile unknowns with a unique remote match.

        Ambiguous cases are left in ``needs_review`` for a human.  Failures are
        retried with a gentle backoff; nothing is resubmitted.
        """
        rows = self.db.query(
            "SELECT id FROM submissions WHERE state='unknown' AND next_poll_at IS NOT NULL "
            "AND next_poll_at<=? ORDER BY next_poll_at LIMIT ?",
            (now(), max(1, limit)),
        )
        processed = 0
        for row in rows:
            processed += 1
            try:
                result = self.reconcile_submission(row["id"], actor="worker")
            except Exception:
                log.exception("reconcile failed for %s", row["id"])
                result = {"state": SUBMISSION_UNKNOWN}
            if result.get("state") == SUBMISSION_UNKNOWN:
                self._schedule_reconcile(row["id"], 60)
        return processed

    def recover_orphans(self) -> int:
        cutoff = now() - self.settings.lease_seconds
        rows = self.db.query(
            "SELECT * FROM submissions WHERE state='submitting' AND updated_at<?", (cutoff,)
        )
        for submission in rows:
            self._mark_unknown(
                submission,
                "worker 在提交过程中中断，无法确认远端是否受理；已冻结并由人工/对账处理",
                {"recovery": True},
            )
        return len(rows)

    def quota_snapshot(self, task_id: str) -> dict:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return {}
        config = json_loads(task["config_json"], {})
        last = self._last_send()
        interval = int(config.get("min_submit_interval_sec") or 300)
        next_allowed = (last + interval) if last else now()
        day_start = self._day_start()
        return {
            "next_submit_at": max(next_allowed, now()) if self._sent_since(day_start, task_id) < int(
                config.get("max_submissions_per_day") or 10**9
            ) else next_day_start(),
            "min_submit_interval_sec": interval,
            "last_send_at": last,
            "inflight": self._inflight(),
            "max_inflight": config.get("max_inflight"),
            "sends_today": self._sent_since(day_start, task_id),
            "max_submissions_per_day": config.get("max_submissions_per_day"),
            "sends_total": self._sent_since(0, task_id),
            "max_submissions_per_task": config.get("max_submissions_per_task"),
            "day": shanghai_day(),
            "unknown": self._unknown_count(),
        }
