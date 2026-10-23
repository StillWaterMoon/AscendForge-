import tempfile
import unittest
from pathlib import Path

from ascendforge.experiment_service import active_experiment
from ascendforge.util import now

from tests.helpers import make_env, seed_task


def force_poll(env):
    env.db.execute("UPDATE submissions SET next_poll_at=? WHERE state='judging'", (now() - 1,))
    return env.scheduler.poll_due()


def age_sends(env):
    env.db.execute("UPDATE submissions SET sent_at=sent_at-120 WHERE sent_at IS NOT NULL")


class GeneralLoopTests(unittest.TestCase):
    def test_compile_failure_repair_then_baseline_building_on_another_task(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-general-loop-"))
        script = [
            {
                "tool_name": "create_candidate",
                "arguments": {
                    "files": [{"path": "kernel.asc", "content": "int f() { return 1; }\n"}],
                    "hypothesis": "first implementation",
                },
            },
            {"tool_name": "request_submission", "arguments": {}},
            {
                "tool_name": "create_candidate",
                "arguments": {
                    "files": [{"path": "kernel.asc", "content": "int f() { return 2; }\n"}],
                    "hypothesis": "repair the compile error from the working source",
                },
            },
            {"tool_name": "request_submission", "arguments": {}},
            {"tool_name": "finish_task", "arguments": {"reason": "target_reached", "summary": "second candidate passed"}},
        ]
        env = make_env(tmp, script=script)
        task = seed_task(env, status="pending", title="Relu (general loop)", config={"metric": "score", "min_submit_interval_sec": 60})
        env.tasks.start(task["id"])

        self.assertEqual(env.runtime.step(task["id"]), "continue")
        self.assertEqual(env.runtime.step(task["id"]), "waiting")
        first = env.db.query_one("SELECT * FROM submissions ORDER BY created_at LIMIT 1")
        claimed, reservation = env.scheduler.claim_submission(first["id"])
        self.assertTrue(reservation.ok)
        env.scheduler.send_claimed(first["id"])
        env.platform.results[env.platform.submitted[0]["remote"]] = {
            "status": "Compile Error",
            "msg": "simulated compile error",
        }
        force_poll(env)

        self.assertEqual(env.runtime.step(task["id"]), "continue")
        self.assertEqual(env.runtime.step(task["id"]), "waiting")
        second = env.db.query_one("SELECT * FROM submissions WHERE id != ?", (first["id"],))
        age_sends(env)
        claimed, reservation = env.scheduler.claim_submission(second["id"])
        self.assertTrue(reservation.ok, reservation.public())
        env.scheduler.send_claimed(second["id"])
        env.platform.results[env.platform.submitted[1]["remote"]] = {
            "status": "Pass",
            "result": [{"testcase_status": "Pass"}],
            "score": 10,
        }
        force_poll(env)

        refreshed_task = env.tasks.get(task["id"])
        self.assertEqual(refreshed_task["best_attempt_id"], second["attempt_id"])
        experiment = active_experiment(env.db, task["id"])
        self.assertEqual(experiment["baseline_attempt_id"], second["attempt_id"])
        self.assertEqual(experiment["working_attempt_id"], second["attempt_id"])
        self.assertIsNotNone(experiment["experiment_best_id"])
