import tempfile
import unittest
from pathlib import Path

from ascendforge.agent.protocol import Action
from tests.helpers import make_env, seed_attempt, seed_task


GENERATOR = r'''
import argparse, json, os
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
os.makedirs(a.out, exist_ok=True)
with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump({"seed": a.seed, "cases": [{"id": "small", "shape": [2, 2]}]}, f)
print("generated", a.seed)
'''

REFERENCE = r'''
import argparse, json, os
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, required=True)
p.add_argument("--manifest", required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
os.makedirs(a.out, exist_ok=True)
manifest = json.load(open(a.manifest, encoding="utf-8"))
with open(os.path.join(a.out, "reference.json"), "w", encoding="utf-8") as f:
    json.dump({"ok": True, "seed": manifest["seed"], "cases": manifest["cases"]}, f)
print("reference ok")
'''


class LocalValidationTests(unittest.TestCase):
    def test_generator_reference_runs_and_candidate_stays_unavailable(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-validate-"))
        env = make_env(tmp)
        task = seed_task(env, status="running", config={"max_local_seconds": 10})
        attempt = seed_attempt(env, task, source="// candidate")

        create = env.runtime.tool_executor.execute(
            Action(
                tool_name="create_test_generator",
                arguments={
                    "generator_source": GENERATOR,
                    "reference_source": REFERENCE,
                    "seed": 7,
                    "spec": {"shape": [2, 2], "dtype": "float16"},
                    "validation": {"atol": 1e-3},
                },
                action_id="a1",
                task_id=task["id"],
                attempt_id=attempt["id"],
            ),
            task_id=task["id"],
            attempt_id=attempt["id"],
        )
        self.assertTrue(create["ok"], create)
        generator_id = create["data"]["generator_id"]

        run = env.runtime.tool_executor.execute(
            Action(
                tool_name="run_local_validation",
                arguments={"generator_id": generator_id},
                action_id="a2",
                task_id=task["id"],
                attempt_id=attempt["id"],
            ),
            task_id=task["id"],
            attempt_id=attempt["id"],
        )
        self.assertTrue(run["ok"], run)
        validation = run["data"]
        self.assertEqual(validation["status"], "reference_ok", validation)
        self.assertTrue(validation["generator_ok"])
        self.assertTrue(validation["reference_ok"])
        self.assertFalse(validation["candidate_executed"])
        self.assertEqual(validation["candidate_status"], "unavailable")
        self.assertEqual(validation["seed"], 7)
        self.assertIn("limitations", validation)

        row = env.db.query_one("SELECT last_validation_json FROM generators WHERE id=?", (generator_id,))
        self.assertIsNotNone(row["last_validation_json"])
        attempt_row = env.db.query_one("SELECT local_validation_json FROM attempts WHERE id=?", (attempt["id"],))
        self.assertIsNotNone(attempt_row["local_validation_json"])


if __name__ == "__main__":
    unittest.main()
