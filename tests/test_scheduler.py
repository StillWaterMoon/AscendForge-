import tempfile
import threading
import unittest
from pathlib import Path

from ascendforge.scheduler import Scheduler
from ascendforge.states import SUBMISSION_UNKNOWN
from ascendforge.tasks import TaskError
from ascendforge.util import next_day_start, now

from tests.helpers import make_env, seed_attempt, seed_task


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-sched-"))
        self.env = make_env(self.tmp)

    def _running_task(self, **config):
        base = {"max_inflight": 2, "min_submit_interval_sec": 60}
        base.update(config)
        task = seed_task(self.env, status="running", config=base)
        return task

    def _queue(self, task, source="// candidate"):
        attempt = seed_attempt(self.env, task, source=source)
        submission = self.env.scheduler.request_submission(task["id"], attempt["id"])
        return attempt, submission

    def test_pausing_task_cannot_queue_new_submission(self):
        task = self._running_task(codex_pause_requested=True)
        attempt = seed_attempt(self.env, task, source="// paused")
        with self.assertRaises(TaskError) as raised:
            self.env.scheduler.request_submission(task["id"], attempt["id"])
        self.assertEqual(raised.exception.code, "TASK_PAUSING")

    def test_claim_send_and_interval_persist_across_restart(self):
        task = self._running_task()
        attempt, submission = self._queue(task)
        claimed, reservation = self.env.scheduler.claim_submission(submission["id"])
        self.assertTrue(reservation.ok, reservation.public())
        self.assertEqual(claimed["state"], "submitting")
        result = self.env.scheduler.send_claimed(submission["id"])
        self.assertEqual(result["state"], "judging")
        self.assertEqual(len(self.env.platform.submitted), 1)

        attempt2, submission2 = self._queue(task, "// candidate 2")
        claimed2, reservation2 = self.env.scheduler.claim_submission(submission2["id"])
        self.assertIsNone(claimed2)
        self.assertEqual(reservation2.reason, "min_interval")

        # A new Scheduler instance (simulating a restart) must preserve the interval.
        restarted = Scheduler(self.env.db, self.env.settings, self.env.platform, self.env.tasks)
        claimed3, reservation3 = restarted.claim_submission(submission2["id"])
        self.assertIsNone(claimed3)
        self.assertEqual(reservation3.reason, "min_interval")
        self.assertIsNotNone(reservation3.retry_at)

    def test_duplicate_claim_is_atomic(self):
        task = self._running_task(max_inflight=3, min_submit_interval_sec=60)
        _, submission = self._queue(task)
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait()
            claimed, reservation = self.env.scheduler.claim_submission(submission["id"])
            outcomes.append((claimed is not None, reservation.reason))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for ok, _ in outcomes if ok), 1, outcomes)
        self.assertEqual(self.env.db.query_one("SELECT state FROM submissions WHERE id=?", (submission["id"],))["state"], "submitting")

    def test_unknown_freezes_all_new_submissions_and_manual_resolution_unblocks(self):
        task = self._running_task()
        attempt, submission = self._queue(task)
        self.env.db.execute(
            "UPDATE submissions SET state='unknown', unknown_reason='timeout', sent_at=? WHERE id=?",
            (now(), submission["id"]),
        )
        self.env.db.execute("UPDATE attempts SET status='submission_unknown' WHERE id=?", (attempt["id"],))
        attempt2 = seed_attempt(self.env, task, source="// second")
        with self.assertRaises(TaskError) as ctx:
            self.env.scheduler.request_submission(task["id"], attempt2["id"])
        self.assertEqual(ctx.exception.code, "SUBMISSION_UNKNOWN")

        resolved = self.env.scheduler.resolve_manual(
            submission["id"], action="mark_failed", note="confirmed not accepted", actor="test"
        )
        self.assertEqual(resolved["state"], "failed")
        attempt3 = seed_attempt(self.env, task, source="// third")
        queued = self.env.scheduler.request_submission(task["id"], attempt3["id"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(self.env.tasks.get(task["id"])["status"], "running")

    def test_unique_reconciliation_adopts_remote_and_finishes(self):
        task = self._running_task()
        attempt, submission = self._queue(task)
        self.env.db.execute(
            "UPDATE submissions SET state='unknown', sent_at=? WHERE id=?",
            (now(), submission["id"]),
        )
        self.env.db.execute("UPDATE attempts SET status='submission_unknown' WHERE id=?", (attempt["id"],))
        self.env.tasks.mark_needs_review(task["id"], "unknown")
        self.env.platform.find_recent = lambda problem_id, user_id, since: [
            {"_id": "remote-reconciled", "user_id": "judge-user-1"}
        ]
        self.env.platform.results["remote-reconciled"] = {
            "status": "Pass",
            "result": [{"testcase_status": "Pass"}],
            "score": 5,
        }
        result = self.env.scheduler.reconcile_submission(submission["id"])
        self.assertEqual(result["state"], "finished")
        row = self.env.db.query_one("SELECT state, remote_id FROM submissions WHERE id=?", (submission["id"],))
        self.assertEqual(row["state"], "finished")
        self.assertEqual(row["remote_id"], "remote-reconciled")
        task_row = self.env.tasks.get(task["id"])
        self.assertEqual(task_row["status"], "running")
        self.assertIsNotNone(task_row["best_attempt_id"])

    def test_ambiguous_reconciliation_stays_needs_review(self):
        task = self._running_task()
        attempt, submission = self._queue(task)
        self.env.db.execute("UPDATE submissions SET state='unknown', sent_at=? WHERE id=?", (now(), submission["id"]))
        self.env.tasks.mark_needs_review(task["id"], "unknown")
        self.env.platform.find_recent = lambda problem_id, user_id, since: [
            {"_id": "remote-a", "user_id": "judge-user-1"},
            {"_id": "remote-b", "user_id": "judge-user-1"},
        ]
        result = self.env.scheduler.reconcile_submission(submission["id"])
        self.assertEqual(result["state"], SUBMISSION_UNKNOWN)
        self.assertEqual(self.env.tasks.get(task["id"])["status"], "needs_review")

    def test_daily_quota_uses_shanghai_day(self):
        task = self._running_task(max_submissions_per_day=1, max_inflight=3)
        attempt, submission = self._queue(task)
        day_start = next_day_start() - 86400
        self.env.db.execute(
            "UPDATE submissions SET sent_at=? WHERE id=?",
            (day_start + 10, submission["id"]),
        )
        attempt2, submission2 = self._queue(task, "// 2")
        claimed, reservation = self.env.scheduler.claim_submission(submission2["id"])
        self.assertIsNone(claimed)
        self.assertEqual(reservation.reason, "max_submissions_per_day")
        self.assertIsNotNone(reservation.retry_at)

    def test_stall_counts_only_remote_results(self):
        task = self._running_task()
        for _ in range(3):
            attempt = seed_attempt(self.env, task, source="// infra failure")
            self.env.db.execute(
                "UPDATE attempts SET status='failed', error_code='MODEL_REJECTED' WHERE id=?",
                (attempt["id"],),
            )
        result_attempt = seed_attempt(self.env, task, source="// real result")
        self.env.db.execute(
            "UPDATE attempts SET status='finished', result_json='{}' WHERE id=?",
            (result_attempt["id"],),
        )
        self.assertEqual(self.env.scheduler._stall_rounds(self.env.tasks.get(task["id"])), 1)

    def test_inflight_limit_is_global(self):
        task = self._running_task(max_inflight=1)
        _, submission = self._queue(task)
        self.env.scheduler.claim_submission(submission["id"])
        self.env.scheduler.send_claimed(submission["id"])
        attempt2, submission2 = self._queue(task, "// 2")
        claimed, reservation = self.env.scheduler.claim_submission(submission2["id"])
        self.assertIsNone(claimed)
        self.assertEqual(reservation.reason, "max_inflight")


if __name__ == "__main__":
    unittest.main()
