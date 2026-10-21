import tempfile
import unittest
from pathlib import Path

from ascendforge.baseline import create_baseline_submission
from ascendforge.experiments import snapshot_files
from ascendforge.tasks import TaskError

from tests.helpers import make_env, seed_task


class BaselineTests(unittest.TestCase):
    def test_baseline_uses_editable_template_and_queues_once(self):
        env = make_env(Path(tempfile.mkdtemp(prefix="af-baseline-")))
        task = seed_task(env, status="pending", config={"min_submit_interval_sec": 60})
        result = create_baseline_submission(env.db, env.settings, env.tasks, env.scheduler, task["id"])
        self.assertEqual(env.tasks.get(task["id"])["status"], "running")
        self.assertEqual(result["submission"]["state"], "queued")
        self.assertEqual(result["attempt"]["provider"], "manual")
        files = snapshot_files(env.db, env.settings, result["snapshot"]["id"])
        self.assertEqual(files, {"kernel.asc": "// template kernel\n"})
        with self.assertRaises(TaskError) as ctx:
            create_baseline_submission(env.db, env.settings, env.tasks, env.scheduler, task["id"])
        self.assertEqual(ctx.exception.code, "BASELINE_EXISTS")

        claimed, reservation = env.scheduler.claim_submission(result["submission"]["id"])
        self.assertTrue(reservation.ok, reservation.public())
        env.scheduler.send_claimed(result["submission"]["id"])
        env.db.execute("UPDATE submissions SET next_poll_at=? WHERE id=?", (0, result["submission"]["id"]))
        env.scheduler.poll_due()
        task_row = env.tasks.get(task["id"])
        self.assertEqual(task_row["status"], "completed")
        self.assertEqual(task_row["stop_reason"], "manual_baseline_finished")
        self.assertIsNotNone(task_row["best_attempt_id"])


if __name__ == "__main__":
    unittest.main()
