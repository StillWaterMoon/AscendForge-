"""Background worker.

The worker is intentionally a separate process from the web server.  It owns
model calls, submissions and polling, and it can be restarted independently.
All state needed to resume lives in SQLite and on disk; no browser connection is
involved.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any, Callable

from .agent.runtime import AgentRuntime
from .config import Settings
from .db import Database
from .execution.capabilities import detect_capabilities
from .experiments import add_event
from .platform.service import PlatformService
from .scheduler import Scheduler
from .security import CredentialStore, SecretBox, load_master_key, purge_expired_sessions
from .states import TASK_RUNNING
from .tasks import TaskService
from .util import json_loads, now

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, *, provider_factory: Callable | None = None):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.init()
        self.box = SecretBox(load_master_key(settings.key_file, ""))
        self.credentials = CredentialStore(self.db, self.box)
        self.platform = PlatformService(self.db, settings, self.credentials)
        self.tasks = TaskService(self.db, settings, self.platform)
        self.scheduler = Scheduler(self.db, settings, self.platform, self.tasks)
        self.runtime = AgentRuntime(
            self.db,
            settings,
            self.platform,
            self.tasks,
            self.scheduler,
            self.credentials,
            provider_factory=provider_factory,
        )
        self.stop_event = threading.Event()
        self._last_recovery = 0.0
        self._last_reconcile = 0.0
        self._last_poll = 0.0

    # ------------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def handler(signum, frame):
            log.info("worker received signal %s; stopping after current step", signum)
            self.stop_event.set()

        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), handler)
                except (ValueError, OSError):
                    pass

    def heartbeat(self, state: str = "idle") -> None:
        self.db.setting_set(
            "worker_heartbeat",
            {
                "worker_id": self.settings.worker_id,
                "state": state,
                "at": now(),
                "platform": detect_capabilities(self.settings).get("candidate_execution"),
            },
        )

    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        self.install_signal_handlers()
        log.info("AscendForge worker %s started", self.settings.worker_id)
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("worker loop error; continuing")
                self.stop_event.wait(3)
                continue
            self.stop_event.wait(max(0.2, self.settings.worker_poll_seconds))
        self.heartbeat("stopped")
        log.info("AscendForge worker stopped")

    def run_once(self) -> dict:
        stats = {
            "orphans": 0,
            "reconciled": 0,
            "polled": 0,
            "sent": 0,
            "agent_steps": 0,
        }
        current = time.monotonic()
        if current - self._last_recovery > 15:
            stats["orphans"] = self.scheduler.recover_orphans()
            self._last_recovery = current
        if current - self._last_reconcile > 10:
            stats["reconciled"] = self.scheduler.reconcile_due()
            self._last_reconcile = current
        if current - self._last_poll > 2:
            stats["polled"] = self.scheduler.poll_due()
            self._last_poll = current
        stats["sent"] = self.process_queued_submissions()
        stats["agent_steps"] = self.run_agent_steps()
        purge_expired_sessions(self.db)
        self.heartbeat("running")
        return stats

    # ------------------------------------------------------------------
    def process_queued_submissions(self, limit: int = 3) -> int:
        rows = self.db.query(
            "SELECT id FROM submissions WHERE state='queued' ORDER BY created_at ASC LIMIT ?", (limit,)
        )
        sent = 0
        for row in rows:
            submission = self.db.query_one("SELECT * FROM submissions WHERE id=?", (row["id"],))
            if not submission:
                continue
            task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (submission["task_id"],))
            if not task:
                continue
            if task["status"] != TASK_RUNNING:
                continue
            try:
                self.platform.adapter()
            except Exception:
                self.tasks.mark_needs_login(task["id"], "CANNJudge 未连接或会话丢失，已暂停提交")
                break
            claimed, reservation = self.scheduler.claim_submission(row["id"])
            if not claimed:
                reason = reservation.reason
                if reason in ("min_interval", "max_submissions_per_day", "max_inflight"):
                    if reservation.retry_at:
                        self.tasks.set_next_action(task["id"], reservation.retry_at)
                    break
                if reason == "global_submission_unknown":
                    self.tasks.mark_needs_review(
                        task["id"], "账号存在结果未知的提交，冻结新提交，必须先对账或人工确认"
                    )
                    break
                if reason in ("max_submissions_per_task", "max_runtime"):
                    self.tasks.complete(task["id"], f"budget:{reason}")
                    continue
                if reason.startswith("task_not_running"):
                    continue
                log.warning("submission %s not claimed: %s", row["id"], reason)
                break
            log.info("sending submission %s for task %s", row["id"], task["id"])
            try:
                result = self.scheduler.send_claimed(row["id"])
                log.info("submission %s result: %s", row["id"], result.get("state"))
                if result.get("state") in ("judging", "unknown", "failed"):
                    sent += 1
            except Exception:
                log.exception("send_claimed failed for %s", row["id"])
            break  # one send per tick; minimum interval handles the rest
        return sent

    # ------------------------------------------------------------------
    def run_agent_steps(self, limit: int = 2) -> int:
        steps = 0
        rows = self.db.query(
            "SELECT id FROM tasks WHERE status=? AND next_action_at<=? ORDER BY next_action_at ASC LIMIT ?",
            (TASK_RUNNING, now(), limit),
        )
        for row in rows:
            if self.stop_event.is_set():
                break
            task_id = row["id"]
            task_row = self.db.query_one("SELECT config_json FROM tasks WHERE id=?", (task_id,))
            config = json_loads(task_row.get("config_json"), {}) if task_row else {}
            if config.get("parameter_runner_active") or config.get("execution_mode") in ("cli", "codex"):
                # The external runner owns attempt creation for this task; the
                # built-in Agent must not race it.
                continue
            try:
                lease = self._acquire_lease(task_id)
                if not lease:
                    continue
                result = self.runtime.step(task_id)
                steps += 1
                if result in ("waiting", "no_attempt", "not_running"):
                    self.tasks.set_next_action(task_id, now() + 5) if result == "waiting" else None
                elif result in ("continue", "invalid_action", "no_action"):
                    self.tasks.set_next_action(task_id, now() + 1)
                elif result.startswith("provider_error"):
                    pass  # runtime set the backoff
                elif result in ("needs_model", "skills_missing", "finished", "step_limit", "no_effective_change"):
                    pass
                log.info("agent step task=%s result=%s", task_id, result)
            except Exception:
                log.exception("agent step failed for task %s", task_id)
                self.tasks.set_next_action(task_id, now() + 30, error_code="WORKER_ERROR", error="agent step crashed")
            finally:
                self._release_lease(task_id)
        return steps

    def _acquire_lease(self, task_id: str) -> bool:
        current = now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT worker_lease_until, worker_id FROM tasks WHERE id=? AND status=?",
                (task_id, TASK_RUNNING),
            ).fetchone()
            if not row:
                return False
            if row["worker_lease_until"] and row["worker_lease_until"] > current and row["worker_id"] != self.settings.worker_id:
                return False
            conn.execute(
                "UPDATE tasks SET worker_id=?, worker_lease_until=?, updated_at=? WHERE id=?",
                (self.settings.worker_id, current + max(300, self.settings.lease_seconds), current, task_id),
            )
        return True

    def _release_lease(self, task_id: str) -> None:
        self.db.execute(
            "UPDATE tasks SET worker_lease_until=NULL WHERE id=? AND worker_id=?",
            (task_id, self.settings.worker_id),
        )


def main() -> int:
    from .cli import main as cli_main

    return cli_main(["worker"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
