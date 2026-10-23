import json
import tempfile
import unittest
from pathlib import Path

from ascendforge.agent.preflight import preflight_candidate
from ascendforge.agent.prompts import _brief_problem
from ascendforge.agent.strategy import derive_active_strategy

from tests.helpers import make_env, seed_attempt, seed_task


class PreflightTests(unittest.TestCase):
    def test_rejects_known_global_tensor_cast(self):
        result = preflight_candidate(
            {"kernel.asc": "auto g = reinterpret_cast<AscendC::GlobalTensor<int8_t>>(x);\n"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "GLOBALTENSOR_REINTERPRET_CAST")

    def test_rejects_missing_ands(self):
        result = preflight_candidate({"kernel.asc": "AscendC::Ands(dst, src, 1, n);\n"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "ASCENDC_ANDS_UNAVAILABLE")

    def test_rejects_scalar_mask_operand(self):
        result = preflight_candidate(
            {"kernel.asc": "AscendC::And(dst, src, (uint16_t)0x7FFF, count);\n"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "ASCENDC_AND_OR_SCALAR_OPERAND")

    def test_clean_candidate_passes(self):
        result = preflight_candidate(
            {"kernel.asc": "AscendC::GlobalTensor<int8_t> g; g.SetGlobalBuffer((__gm__ int8_t*)x, n);\n"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])

    def test_single_dtype_warning_when_contract_has_many(self):
        problem = {"desc": "dtype: int8, int16, int32, float16, float32"}
        result = preflight_candidate({"kernel.asc": "int8_t value;\n"}, problem)
        self.assertTrue(result["ok"])
        self.assertTrue(any("multiple dtypes" in item for item in result["warnings"]))


class ProblemBriefTests(unittest.TestCase):
    def test_cannjudge_desc_field_is_included(self):
        text = _brief_problem({"name": "copysign", "desc": "hidden problem description"})
        self.assertIn("hidden problem description", text)


class ParentContextTests(unittest.TestCase):
    def test_parent_source_is_injected(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-parent-context-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        parent = seed_attempt(env, task, source="// unique parent implementation\n")
        normalized = {
            "outcome": "failed",
            "raw_status": "Wrong Answer",
            "testcase_passed": 1,
            "testcase_total": 2,
            "precision_ratio": 0.25,
            "testcases": [
                {"index": 0, "status": "Pass", "passed": True, "precision_ratio": 1, "raw": {"best_time": 1.2}},
                {"index": 1, "status": "Wrong Answer", "passed": False, "precision_ratio": 0.25, "raw": {"best_time": 1.3}},
            ],
        }
        env.db.execute(
            "UPDATE attempts SET normalized_json=?, result_json=? WHERE id=?",
            (json.dumps(normalized), json.dumps({"status": "Wrong Answer", "result": [{"precision_ratio": 0.25}]}), parent["id"]),
        )
        child = seed_attempt(env, task, parent=parent["id"], source="// child implementation\n")
        messages = env.runtime._build_messages(env.tasks.get(task["id"]), child, "system")
        context = messages[1]["content"]
        self.assertIn("## Parent candidate source", context)
        self.assertIn("unique parent implementation", context)
        self.assertIn("## Parent evaluation result (full", context)
        self.assertIn("full_raw_json", context)
        self.assertIn("precision_ratio=0.25", context)
        self.assertIn("minimal diff", context)


class TestcaseDigestTests(unittest.TestCase):
    def test_per_testcase_precision_is_injected(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-testcase-digest-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        attempt = seed_attempt(env, task)
        normalized = {
            "outcome": "failed",
            "raw_status": "Wrong Answer",
            "testcase_passed": 0,
            "testcase_total": 2,
            "precision_ratio": 0.25,
            "testcases": [
                {"index": 0, "status": "Wrong Answer", "precision_ratio": 0.4807, "time": 10.17, "raw": {"best_time": 1.49}},
                {"index": 1, "status": "Wrong Answer", "precision_ratio": 0.25, "time": 18.34, "raw": {"best_time": 1.46}},
            ],
        }
        env.db.execute(
            "UPDATE attempts SET normalized_json=? WHERE id=?",
            (json.dumps(normalized), attempt["id"]),
        )
        messages = env.runtime._build_messages(env.tasks.get(task["id"]), attempt, "system")
        context = messages[1]["content"]
        self.assertIn("## Latest remote feedback (per testcase)", context)
        self.assertIn("per-testcase:", context)
        self.assertIn("0.4807", context)
        self.assertIn("precision_ratio is a platform field", context)


class OperatorMessageTests(unittest.TestCase):
    def test_operator_message_is_injected(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-operator-message-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        env.tasks.add_message(task["id"], "下一轮先确认 int16 的符号位", author="admin")
        attempt = seed_attempt(env, task)
        messages = env.runtime._build_messages(env.tasks.get(task["id"]), attempt, "system")
        context = messages[1]["content"]
        self.assertIn("## Operator messages", context)
        self.assertIn("下一轮先确认 int16 的符号位", context)


class ActiveStrategyTests(unittest.TestCase):
    def test_phase_changes_from_compile_to_correctness_to_performance(self):
        compile_attempt = {
            "id": "a1",
            "ordinal": 3,
            "normalized": {"outcome": "compile_error", "raw_status": "Compile Error"},
        }
        baseline = derive_active_strategy({"best_attempt_id": None}, [])
        self.assertEqual(baseline["phase"], "baseline")
        compile_phase = derive_active_strategy({"best_attempt_id": None}, [compile_attempt])
        self.assertEqual(compile_phase["phase"], "compile")
        wrong_attempt = {
            "id": "a2",
            "ordinal": 4,
            "normalized": {"outcome": "failed", "raw_status": "Wrong Answer", "testcase_passed": 6, "testcase_total": 15},
        }
        wrong_phase = derive_active_strategy({"best_attempt_id": None}, [wrong_attempt])
        self.assertEqual(wrong_phase["phase"], "correctness")
        perf = derive_active_strategy({"best_attempt_id": "best"}, [{"id": "best", "ordinal": 9}])
        self.assertEqual(perf["phase"], "performance")
        self.assertIn("best", perf["text"])

    def test_active_strategy_is_injected(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-active-strategy-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        attempt = seed_attempt(env, task)
        messages = env.runtime._build_messages(env.tasks.get(task["id"]), attempt, "system")
        context = messages[1]["content"]
        self.assertIn("## Active strategy", context)
        self.assertIn("建立基线", context)


class StrategyConfigTests(unittest.TestCase):
    def test_strategy_prompt_is_validated_and_kept(self):
        from ascendforge.tasks import TaskError, normalize_config

        config = normalize_config({"strategy_prompt": "先覆盖 dtype，再优化分块"})
        self.assertEqual(config["strategy_prompt"], "先覆盖 dtype，再优化分块")
        with self.assertRaises(TaskError):
            normalize_config({"strategy_prompt": "x" * 8001})

    def test_strategy_is_injected_into_context(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-strategy-context-"))
        env = make_env(tmp)
        task = seed_task(env, status="running", config={"strategy_prompt": "本题重点：dtype 全覆盖"})
        attempt = seed_attempt(env, task)
        messages = env.runtime._build_messages(env.tasks.get(task["id"]), attempt, "system")
        context = messages[1]["content"]
        self.assertIn("## Problem-specific strategy", context)
        self.assertIn("dtype 全覆盖", context)
