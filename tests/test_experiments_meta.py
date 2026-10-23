import tempfile
import unittest
from pathlib import Path

from ascendforge.experiment_service import (
    active_experiment,
    ensure_active,
    ensure_legacy,
    note_result,
    set_working_revision,
)
from ascendforge.experiments import create_attempt

from tests.helpers import make_env, seed_attempt, seed_task


class ExperimentServiceTests(unittest.TestCase):
    def test_legacy_attempts_are_grouped_and_new_experiment_is_fixed(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-experiment-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        first = seed_attempt(env, task, source="// v1")
        second = seed_attempt(env, task, parent=first["id"], source="// v2")
        third = seed_attempt(env, task, parent=second["id"], source="// v3")
        env.db.execute(
            "UPDATE tasks SET best_attempt_id=?, best_metric_json=? WHERE id=?",
            (second["id"], "{}", task["id"]),
        )

        legacy = ensure_legacy(env.db, task["id"])
        self.assertIsNotNone(legacy)
        self.assertEqual(
            env.db.scalar("SELECT COUNT(*) FROM attempts WHERE task_id=? AND experiment_id=?", (task["id"], legacy["id"])),
            3,
        )

        active = ensure_active(env.db, task["id"])
        self.assertNotEqual(active["id"], legacy["id"])
        self.assertEqual(active["baseline_attempt_id"], second["id"])
        self.assertEqual(active["working_attempt_id"], second["id"])
        self.assertEqual(ensure_active(env.db, task["id"])["id"], active["id"])

        revision = create_attempt(
            env.db,
            task["id"],
            parent_attempt_id=active["working_attempt_id"],
            experiment_id=active["id"],
        )
        self.assertEqual(revision["experiment_id"], active["id"])
        set_working_revision(env.db, active["id"], revision["id"])
        refreshed = active_experiment(env.db, task["id"])
        self.assertEqual(refreshed["working_attempt_id"], revision["id"])

        note_result(env.db, active["id"], revision["id"], {"all_passed": True})
        refreshed = active_experiment(env.db, task["id"])
        self.assertEqual(refreshed["experiment_best_id"], revision["id"])

    def test_no_attempts_means_no_legacy_group(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-experiment-empty-"))
        env = make_env(tmp)
        task = seed_task(env, status="pending")
        self.assertIsNone(ensure_legacy(env.db, task["id"]))
        active = ensure_active(env.db, task["id"])
        self.assertEqual(active["status"], "active")
        self.assertIsNone(active["baseline_attempt_id"])
