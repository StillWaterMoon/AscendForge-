import tempfile
import unittest
from pathlib import Path

from ascendforge.platform.base import PlatformError, SubmissionUnknown
from ascendforge.util import now

from tests.helpers import make_env, seed_attempt, seed_task


def _queued(env, task):
    attempt = seed_attempt(env, task, source="// candidate\n")
    submission = env.scheduler.request_submission(task["id"], attempt["id"])
    claimed, reservation = env.scheduler.claim_submission(submission["id"])
    assert claimed and reservation.ok, reservation.public()
    return attempt, submission


class SubmissionErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-sub-err-"))
        self.env = make_env(self.tmp)

    def _task(self):
        return seed_task(
            self.env,
            status="running",
            config={"min_submit_interval_sec": 60, "max_inflight": 1},
        )

    def test_auth_expired_pauses_submissions(self):
        task = self._task()
        attempt, submission = _queued(self.env, task)

        def expired(*args, **kwargs):
            raise PlatformError("AUTH_EXPIRED", "session expired", http_status=401)

        self.env.platform.submit = expired
        result = self.env.scheduler.send_claimed(submission["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.env.tasks.get(task["id"])["status"], "needs_login")
        row = self.env.db.query_one("SELECT state, error_code FROM submissions WHERE id=?", (submission["id"],))
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "AUTH_EXPIRED")
        self.assertEqual(
            self.env.db.query_one("SELECT status FROM attempts WHERE id=?", (attempt["id"],))["status"],
            "failed",
        )

    def test_forbidden_requires_review(self):
        task = self._task()
        _attempt, submission = _queued(self.env, task)

        def forbidden(*args, **kwargs):
            raise PlatformError("FORBIDDEN", "no permission", http_status=403)

        self.env.platform.submit = forbidden
        self.env.scheduler.send_claimed(submission["id"])
        task_row = self.env.tasks.get(task["id"])
        self.assertEqual(task_row["status"], "needs_review")
        self.assertEqual(task_row["last_error_code"], "FORBIDDEN")

    def test_rate_limit_backoff_and_quota_consumed(self):
        task = self._task()
        _attempt, submission = _queued(self.env, task)

        def limited(*args, **kwargs):
            raise PlatformError(
                "RATE_LIMITED",
                "slow down",
                retryable=True,
                http_status=429,
                details={"retry_after": "12"},
            )

        self.env.platform.submit = limited
        before = now()
        self.env.scheduler.send_claimed(submission["id"])
        task_row = self.env.tasks.get(task["id"])
        self.assertEqual(task_row["status"], "running")
        self.assertGreaterEqual(task_row["next_action_at"], before + 5)
        row = self.env.db.query_one("SELECT state, sent_at FROM submissions WHERE id=?", (submission["id"],))
        self.assertEqual(row["state"], "failed")
        self.assertIsNotNone(row["sent_at"])

    def test_unknown_freezes_and_marks_review(self):
        task = self._task()
        attempt, submission = _queued(self.env, task)

        def unknown(*args, **kwargs):
            raise SubmissionUnknown("timeout, remote may have accepted")

        self.env.platform.submit = unknown
        result = self.env.scheduler.send_claimed(submission["id"])
        self.assertEqual(result["state"], "unknown")
        task_row = self.env.tasks.get(task["id"])
        self.assertEqual(task_row["status"], "needs_review")
        self.assertEqual(task_row["last_error_code"], "SUBMISSION_UNKNOWN")
        row = self.env.db.query_one("SELECT state, unknown_reason FROM submissions WHERE id=?", (submission["id"],))
        self.assertEqual(row["state"], "unknown")
        self.assertIn("timeout", row["unknown_reason"])
        self.assertEqual(
            self.env.db.query_one("SELECT status FROM attempts WHERE id=?", (attempt["id"],))["status"],
            "submission_unknown",
        )


if __name__ == "__main__":
    unittest.main()
