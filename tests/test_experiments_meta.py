import tempfile
import unittest
from pathlib import Path

from ascendforge.experiment_service import (
    active_experiment,
    current_context,
    ensure_active,
    ensure_legacy,
    note_result,
    set_working_revision,
    start_experiment,
)
from ascendforge.experiments import create_attempt, update_performance_best

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

    def test_slower_passing_candidate_does_not_replace_experiment_best(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-experiment-slower-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        baseline = seed_attempt(env, task, source="// baseline")
        env.db.execute(
            "UPDATE tasks SET best_attempt_id=?, best_metric_json=? WHERE id=?",
            (baseline["id"], "{}", task["id"]),
        )
        experiment = ensure_active(env.db, task["id"])
        candidate = seed_attempt(env, task, parent=baseline["id"], source="// slower")
        env.db.execute(
            "UPDATE attempts SET experiment_id=?, normalized_json=? WHERE id=?",
            (experiment["id"], '{"all_passed": true, "time_values": [30, 40]}', candidate["id"]),
        )
        env.db.execute(
            "UPDATE attempts SET normalized_json=? WHERE id=?",
            ('{"all_passed": true, "time_values": [10, 20]}', baseline["id"]),
        )
        self.assertFalse(note_result(env.db, experiment["id"], candidate["id"], {"all_passed": True}))
        refreshed = active_experiment(env.db, task["id"])
        self.assertIsNone(refreshed["experiment_best_id"])

    def test_performance_best_compares_time_not_precision(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-performance-"))
        env = make_env(tmp)
        task = seed_task(env, status="running", config={"metric": "auto"})
        first = seed_attempt(env, task, source="// slow")
        second = seed_attempt(env, task, source="// slower")
        third = seed_attempt(env, task, source="// fast")
        self.assertIsNotNone(
            update_performance_best(
                env.db,
                task["id"],
                first["id"],
                {"all_passed": True, "time_values": [10, 20]},
                {"metric": "auto"},
            )
        )
        self.assertIsNone(
            update_performance_best(
                env.db,
                task["id"],
                second["id"],
                {"all_passed": True, "time_values": [15, 25]},
                {"metric": "auto"},
            )
        )
        self.assertIsNotNone(
            update_performance_best(
                env.db,
                task["id"],
                third["id"],
                {"all_passed": True, "time_values": [5, 8]},
                {"metric": "auto"},
            )
        )
        row = env.db.query_one("SELECT performance_best_attempt_id FROM tasks WHERE id=?", (task["id"],))
        self.assertEqual(row["performance_best_attempt_id"], third["id"])
        self.assertIsNone(
            update_performance_best(
                env.db,
                task["id"],
                first["id"],
                {"all_passed": True, "precision_ratio": 1.0},
                {"metric": "auto"},
            )
        )

    def test_start_experiment_records_fixed_baseline_and_plan(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-experiment-start-"))
        env = make_env(tmp)
        task = seed_task(env, status="paused")
        baseline = seed_attempt(env, task, source="// passing")
        env.db.execute(
            "UPDATE attempts SET normalized_json=? WHERE id=?",
            ('{"all_passed": true, "time_values": [10]}', baseline["id"]),
        )
        env.db.execute(
            "UPDATE tasks SET best_attempt_id=?, performance_best_attempt_id=? WHERE id=?",
            (baseline["id"], baseline["id"], task["id"]),
        )
        old = ensure_active(env.db, task["id"])
        new = start_experiment(
            env.db,
            task["id"],
            hypothesis="tile and depth sweep",
            baseline_attempt_id=baseline["id"],
            search_space={"configs": [{"tile": 8192}, {"tile": 32768}]},
            success_criteria={"all_passed": True},
            budget={"max_revisions": 2},
        )
        refreshed_old = env.db.query_one("SELECT * FROM experiments WHERE id=?", (old["id"],))
        self.assertEqual(refreshed_old["status"], "completed")
        self.assertEqual(refreshed_old["stop_reason"], "superseded_by_new_experiment")
        self.assertEqual(new["baseline_attempt_id"], baseline["id"])
        context = current_context(env.db, task["id"])
        self.assertEqual(context["id"], new["id"])
        self.assertEqual(context["hypothesis"], "tile and depth sweep")
        self.assertIsNotNone(context["search_space"])
        self.assertIsNotNone(context["success_criteria"])
        self.assertIsNotNone(context["budget"])

    def test_first_verified_candidate_establishes_baseline(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-baseline-build-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        experiment = ensure_active(env.db, task["id"])
        self.assertIsNone(experiment["baseline_attempt_id"])
        candidate = seed_attempt(env, task, source="// first correct implementation")
        self.assertTrue(
            note_result(
                env.db,
                experiment["id"],
                candidate["id"],
                {"all_passed": True, "time_values": [10]},
            )
        )
        refreshed = active_experiment(env.db, task["id"])
        self.assertEqual(refreshed["baseline_attempt_id"], candidate["id"])
        self.assertEqual(refreshed["working_attempt_id"], candidate["id"])
        self.assertEqual(refreshed["experiment_best_id"], candidate["id"])

    def test_budget_completion_closes_experiment_incomplete(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-budget-close-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        experiment = ensure_active(env.db, task["id"])
        env.tasks.complete(task["id"], "budget:max_attempts")
        refreshed = env.db.query_one("SELECT * FROM experiments WHERE id=?", (experiment["id"],))
        self.assertEqual(refreshed["status"], "abandoned")
        self.assertEqual(refreshed["stop_reason"], "budget:max_attempts")

    def test_no_attempts_means_no_legacy_group(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-experiment-empty-"))
        env = make_env(tmp)
        task = seed_task(env, status="pending")
        self.assertIsNone(ensure_legacy(env.db, task["id"]))
        active = ensure_active(env.db, task["id"])
        self.assertEqual(active["status"], "active")
        self.assertIsNone(active["baseline_attempt_id"])
