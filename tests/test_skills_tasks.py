import tempfile
import unittest
from pathlib import Path

from ascendforge.agent.skills import SkillRegistry
from ascendforge.tasks import TaskError, normalize_config

from tests.helpers import make_env, seed_attempt, seed_task


from unittest.mock import patch


class SkillTests(unittest.TestCase):
    def test_required_skills_load_explicitly(self):
        registry = SkillRegistry(Path(__file__).resolve().parents[1] / "skills")
        names = {item["name"] for item in registry.list()}
        self.assertEqual(
            names,
            {
                "build-cannjudge-workbench",
                "optimize-operator",
                "optimize-ascend-operator",
                "generate-operator-tests",
                "start-operator-optimization",
            },
        )
        block, meta = registry.prompt_block(
            ["optimize-operator", "optimize-ascend-operator", "generate-operator-tests"]
        )
        self.assertIn("算子迭代优化", block)
        self.assertIn("迭代优化昇腾算子", block)
        self.assertIn("生成算子测试", block)
        self.assertEqual(len(meta), 3)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in meta))

    def test_runtime_prompt_includes_skills(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-skills-"))
        env = make_env(tmp)
        prompt, meta = env.runtime.system_prompt()
        self.assertIn("optimize-operator", prompt)
        self.assertIn("optimize-ascend-operator", prompt)
        self.assertIn("generate-operator-tests", prompt)
        self.assertIn("request_submission", prompt)
        self.assertEqual(len(meta), 3)


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-state-"))
        self.env = make_env(self.tmp)

    def test_lifecycle_and_illegal_transition(self):
        task = seed_task(self.env, status="pending")
        with self.assertRaises(TaskError):
            self.env.tasks.pause(task["id"])
        started = self.env.tasks.start(task["id"])
        self.assertEqual(started["status"], "running")
        paused = self.env.tasks.pause(task["id"])
        self.assertEqual(paused["status"], "paused")
        resumed = self.env.tasks.resume(task["id"])
        self.assertEqual(resumed["status"], "running")
        stopped = self.env.tasks.stop(task["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self.env.tasks.stop(task["id"])["status"], "stopped")

    def test_stop_cancels_queued_submission(self):
        task = seed_task(self.env, status="running", config={"min_submit_interval_sec": 60})
        attempt = seed_attempt(self.env, task, source="// queued")
        submission = self.env.scheduler.request_submission(task["id"], attempt["id"])
        self.assertEqual(submission["state"], "queued")
        self.env.tasks.stop(task["id"], "test stop")
        row = self.env.db.query_one("SELECT state, error_code FROM submissions WHERE id=?", (submission["id"],))
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "TASK_STOPPED")
        attempt_row = self.env.db.query_one("SELECT status FROM attempts WHERE id=?", (attempt["id"],))
        self.assertEqual(attempt_row["status"], "failed")

    def test_pause_terminates_codex_runner_and_clears_pid(self):
        task = seed_task(
            self.env,
            status="running",
            config={"execution_mode": "codex", "codex_runner_pid": 4242, "codex_runner_active": True},
        )
        with patch("ascendforge.tasks.subprocess.run") as run:
            self.env.tasks.pause(task["id"], "test")
            run.assert_called_once()
        refreshed = self.env.tasks.get(task["id"])
        self.assertEqual(refreshed["status"], "paused")
        self.assertFalse(refreshed["config"]["codex_runner_active"])
        self.assertNotIn("codex_runner_pid", refreshed["config"])
        self.assertEqual(refreshed["config"]["codex_runner_status"], "terminated")

    def test_config_validation(self):
        with self.assertRaises(TaskError):
            normalize_config({"max_inflight": 9})
        with self.assertRaises(TaskError):
            normalize_config({"metric": "time"})
        config = normalize_config({"metric": "time", "time_unit": "ms"})
        self.assertEqual(config["metric"], "time")
        unlimited = normalize_config({"max_tokens": 0, "max_model_calls": 0})
        self.assertEqual(unlimited["max_tokens"], 0)
        self.assertEqual(unlimited["max_model_calls"], 0)
        with self.assertRaises(TaskError):
            normalize_config({"max_tokens": -1})
        with self.assertRaises(TaskError):
            normalize_config({"unknown_key": 1})

    def test_interval_is_global_across_tasks(self):
        config = {"min_submit_interval_sec": 300, "max_inflight": 3}
        task_a = seed_task(self.env, status="running", config=config, title="A")
        task_b = seed_task(self.env, status="running", config=config, title="B")
        attempt_a = seed_attempt(self.env, task_a)
        sub_a = self.env.scheduler.request_submission(task_a["id"], attempt_a["id"])
        claimed, reservation = self.env.scheduler.claim_submission(sub_a["id"])
        self.assertTrue(reservation.ok, reservation.public())
        self.env.scheduler.send_claimed(sub_a["id"])

        attempt_b = seed_attempt(self.env, task_b)
        sub_b = self.env.scheduler.request_submission(task_b["id"], attempt_b["id"])
        claimed_b, reservation_b = self.env.scheduler.claim_submission(sub_b["id"])
        self.assertIsNone(claimed_b)
        self.assertEqual(reservation_b.reason, "min_interval")


if __name__ == "__main__":
    unittest.main()
