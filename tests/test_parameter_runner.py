import tempfile
import unittest
from pathlib import Path

from ascendforge.experiment_service import (
    active_experiment,
    ensure_active,
    start_experiment,
)
from ascendforge.experiments import read_snapshot
from ascendforge.parameter_runner import ParameterRunner

from tests.helpers import make_env, seed_attempt, seed_task


SOURCE = """constexpr int64_t kTileBytes = 32768;
constexpr int64_t kDepth = 2;
int f() { return 1; }
"""


class SimulatedEvaluator:
    name = "simulated"

    def __init__(self, fail_first: set[tuple[int, int]] | None = None):
        self.calls = 0
        self.fail_first = fail_first or set()
        self.failed = set()

    def __call__(self, attempt, files, params, eval_key):
        self.calls += 1
        key = (int(params["tile_bytes"]), int(params["depth"]))
        if key in self.fail_first and key not in self.failed:
            self.failed.add(key)
            return {"status": "Compile Error", "msg": "simulated transient compile failure"}
        # Deterministic simulated measurement: larger tile / depth is faster.
        tile = int(params["tile_bytes"])
        depth = int(params["depth"])
        value = 1000.0 - (0 if tile == 8192 else 200) - (0 if depth == 1 else 120)
        return {
            "status": "Pass",
            "result": [{"testcase_status": "Pass", "time": value}],
            "score": 1,
        }


def make_experiment(env, task, *, retest=None, budget=None, configs=None):
    baseline = seed_attempt(env, task, source=SOURCE)
    env.db.execute(
        "UPDATE attempts SET normalized_json=? WHERE id=?",
        ('{"all_passed": true, "testset_passed": true, "coverage_complete": true, "time_values": [1000]}', baseline["id"]),
    )
    env.db.execute(
        "UPDATE tasks SET best_attempt_id=?, performance_best_attempt_id=? WHERE id=?",
        (baseline["id"], baseline["id"], task["id"]),
    )
    ensure_active(env.db, task["id"])
    experiment = start_experiment(
        env.db,
        task["id"],
        hypothesis="parameter sweep",
        baseline_attempt_id=baseline["id"],
        search_space={
            "configs": configs or [
                {"id": "A", "params": {"tile_bytes": 8192, "depth": 1}},
                {"id": "B", "params": {"tile_bytes": 32768, "depth": 1}},
                {"id": "C", "params": {"tile_bytes": 8192, "depth": 2}},
                {"id": "D", "params": {"tile_bytes": 32768, "depth": 2}},
            ],
            "retest": retest or [],
        },
        success_criteria={"all_passed": True},
        budget=budget or {"max_revisions": 8, "max_evaluations": 8},
    )
    return baseline, experiment


class ParameterRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-param-"))
        self.env = make_env(self.tmp)
        self.task = seed_task(self.env, status="paused")

    def test_enumerates_fixed_configs_and_records_evidence(self):
        baseline, experiment = make_experiment(self.env, self.task)
        evaluator = SimulatedEvaluator()
        result = ParameterRunner(
            self.env.db, self.env.settings, self.task["id"], experiment["id"], evaluator
        ).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_configs"], ["A", "B", "C", "D"])
        self.assertEqual(evaluator.calls, 3)  # D is the fixed baseline control
        rows = self.env.db.query(
            "SELECT * FROM attempts WHERE experiment_id=? AND revision_kind='parameter' ORDER BY ordinal",
            (experiment["id"],),
        )
        self.assertEqual(len(rows), 4)
        base_files = read_snapshot(self.env.db, self.env.settings, baseline["snapshot_id"])["files"]
        changed_sources = 0
        for attempt in rows:
            self.assertTrue(attempt["snapshot_id"])
            self.assertTrue(attempt["config_json"])
            files = read_snapshot(self.env.db, self.env.settings, attempt["snapshot_id"])["files"]
            if files["kernel.asc"] != base_files["kernel.asc"]:
                changed_sources += 1
            self.assertIn("kTileBytes", files["kernel.asc"])
        self.assertEqual(changed_sources, 3)  # D is the baseline control
        evals = self.env.db.query("SELECT * FROM evaluations WHERE experiment_id=? ORDER BY created_at", (experiment["id"],))
        self.assertEqual(len(evals), 4)
        self.assertTrue(all(row["result_json"] for row in evals))

    def test_reuses_identical_evaluation(self):
        baseline, experiment = make_experiment(self.env, self.task)
        first = SimulatedEvaluator()
        ParameterRunner(self.env.db, self.env.settings, self.task["id"], experiment["id"], first).run()
        second_baseline, second = make_experiment(self.env, self.task)
        second_eval = SimulatedEvaluator()
        result = ParameterRunner(
            self.env.db, self.env.settings, self.task["id"], second["id"], second_eval
        ).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(second_eval.calls, 0)
        reused = self.env.db.query("SELECT * FROM evaluations WHERE experiment_id=? AND reused=1", (second["id"],))
        self.assertEqual(len(reused), 4)

    def test_failure_repair_retries_fixed_config(self):
        _, experiment = make_experiment(
            self.env,
            self.task,
            budget={"max_revisions": 8, "max_evaluations": 8},
        )
        evaluator = SimulatedEvaluator(fail_first={(32768, 1)})
        result = ParameterRunner(
            self.env.db, self.env.settings, self.task["id"], experiment["id"], evaluator, max_repairs_per_config=1
        ).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(evaluator.calls, 4)
        repairs = self.env.db.query(
            "SELECT * FROM evaluations WHERE experiment_id=? AND kind='parameter_repair'",
            (experiment["id"],),
        )
        self.assertEqual(len(repairs), 1)
        self.assertTrue(repairs[0]["result_json"])

    def test_retest_records_separate_evidence(self):
        _, experiment = make_experiment(self.env, self.task, retest=["D"], budget={"max_revisions": 6, "max_evaluations": 6})
        evaluator = SimulatedEvaluator()
        result = ParameterRunner(self.env.db, self.env.settings, self.task["id"], experiment["id"], evaluator).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(evaluator.calls, 4)
        retests = self.env.db.query(
            "SELECT * FROM evaluations WHERE experiment_id=? AND kind='retest'", (experiment["id"],)
        )
        self.assertEqual(len(retests), 1)
        self.assertTrue(retests[0]["replicate_of"])

    def test_budget_exhaustion_is_incomplete(self):
        _, experiment = make_experiment(self.env, self.task, budget={"max_revisions": 2, "max_evaluations": 4})
        result = ParameterRunner(
            self.env.db, self.env.settings, self.task["id"], experiment["id"], SimulatedEvaluator()
        ).run()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertFalse(result["verified"])
        row = self.env.db.query_one("SELECT status, stop_reason FROM experiments WHERE id=?", (experiment["id"],))
        self.assertEqual(row["status"], "abandoned")
        self.assertEqual(row["stop_reason"], "budget_exhausted")
