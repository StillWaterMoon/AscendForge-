import tempfile
import unittest
from pathlib import Path

from ascendforge.agent.providers import MockProvider, ModelConfig
from ascendforge.experiments import snapshot_files
from ascendforge.util import now

from tests.helpers import make_env, seed_task


def force_poll(env):
    env.db.execute("UPDATE submissions SET next_poll_at=? WHERE state='judging'", (now() - 1,))
    return env.scheduler.poll_due()


def age_sends(env):
    env.db.execute("UPDATE submissions SET sent_at=sent_at-120 WHERE sent_at IS NOT NULL")


class RuntimeLoopTests(unittest.TestCase):
    def test_two_round_mock_optimization_and_failed_third_keeps_best(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-loop-"))
        payload = {
            "provider": "mock",
            "base_url": "https://mock.invalid/v1",
            "api_key": "test",
            "model": "mock",
            "supports_tool_calls": False,
            "max_tokens": 512,
            "temperature": 0.1,
            "timeout": 10,
            "price_prompt_per_1k": 0.001,
            "price_completion_per_1k": 0.002,
        }
        provider = MockProvider(
            ModelConfig.from_payload(payload),
            script=[
                {"tool_name": "create_candidate", "arguments": {"files": [{"path": "kernel.asc", "content": "// v1\n"}], "hypothesis": "baseline"}},
                {"tool_name": "request_submission", "arguments": {}},
                {"tool_name": "create_candidate", "arguments": {"files": [{"path": "kernel.asc", "content": "// v2\n"}], "hypothesis": "tune tiling"}},
                {"tool_name": "request_submission", "arguments": {}},
                {"tool_name": "create_candidate", "arguments": {"files": [{"path": "kernel.asc", "content": "// v3\n"}], "hypothesis": "risky change"}},
                {"tool_name": "request_submission", "arguments": {}},
                {"tool_name": "finish_task", "arguments": {"reason": "no_improvement", "summary": "two passing rounds, third failed"}},
            ],
        )
        env = make_env(tmp, provider=provider)
        task = seed_task(env, status="pending", config={"metric": "score", "min_submit_interval_sec": 60})
        env.tasks.start(task["id"])

        self.assertEqual(env.runtime.step(task["id"]), "continue")
        self.assertEqual(env.runtime.step(task["id"]), "waiting")
        submission1 = env.db.query_one("SELECT * FROM submissions ORDER BY created_at LIMIT 1")
        claimed, reservation = env.scheduler.claim_submission(submission1["id"])
        self.assertTrue(reservation.ok)
        sent = env.scheduler.send_claimed(submission1["id"])
        self.assertEqual(sent["state"], "judging")
        self.assertEqual(len(env.platform.submitted), 1)

        # The submitted bytes are exactly the snapshot bytes.
        attempt1 = env.db.query_one("SELECT * FROM attempts WHERE id=?", (submission1["attempt_id"],))
        self.assertEqual(
            env.platform.submitted[0]["files"],
            snapshot_files(env.db, env.settings, attempt1["snapshot_id"]),
        )

        env.platform.results[env.platform.submitted[0]["remote"]]["score"] = 10
        self.assertEqual(force_poll(env), 1)
        attempt1 = env.db.query_one("SELECT * FROM attempts WHERE id=?", (attempt1["id"],))
        self.assertEqual(attempt1["status"], "finished")
        best1 = env.tasks.get(task["id"])["best_attempt_id"]
        self.assertEqual(best1, attempt1["id"])

        self.assertEqual(env.runtime.step(task["id"]), "continue")
        self.assertEqual(env.runtime.step(task["id"]), "waiting")
        submission2 = env.db.query_one("SELECT * FROM submissions WHERE id != ?", (submission1["id"],))
        age_sends(env)
        claimed, reservation = env.scheduler.claim_submission(submission2["id"])
        self.assertTrue(reservation.ok, reservation.public())
        env.platform.default_result = {
            "status": "Pass",
            "result": [{"testcase_status": "Pass"}],
            "score": 12,
        }
        env.scheduler.send_claimed(submission2["id"])
        force_poll(env)
        task_row = env.tasks.get(task["id"])
        best2 = task_row["best_attempt_id"]
        self.assertNotEqual(best2, best1)
        attempt2 = env.db.query_one("SELECT * FROM attempts WHERE id=?", (best2,))
        self.assertEqual(attempt2["parent_attempt_id"], best1)

        self.assertEqual(env.runtime.step(task["id"]), "continue")
        self.assertEqual(env.runtime.step(task["id"]), "waiting")
        submission3 = env.db.query_one(
            "SELECT * FROM submissions WHERE id NOT IN (?, ?)", (submission1["id"], submission2["id"])
        )
        age_sends(env)
        claimed, reservation = env.scheduler.claim_submission(submission3["id"])
        self.assertTrue(reservation.ok, reservation.public())
        env.platform.default_result = {
            "status": "Fail",
            "result": [{"testcase_status": "Wrong Answer"}],
        }
        env.scheduler.send_claimed(submission3["id"])
        force_poll(env)
        task_row = env.tasks.get(task["id"])
        self.assertEqual(task_row["best_attempt_id"], best2)
        attempt3 = env.db.query_one("SELECT * FROM attempts WHERE id=?", (submission3["attempt_id"],))
        self.assertEqual(attempt3["status"], "finished")
        self.assertEqual(attempt3["error_code"], "FAILED")

        self.assertEqual(env.runtime.step(task["id"]), "finished")
        task_row = env.tasks.get(task["id"])
        self.assertEqual(task_row["status"], "completed")
        self.assertEqual(task_row["best_attempt_id"], best2)
        self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM submissions"), 3)
        self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM model_calls"), 7)


if __name__ == "__main__":
    unittest.main()
