import json
import tempfile
import unittest
from pathlib import Path

from ascendforge.agent.preflight import preflight_candidate

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


class ParentContextTests(unittest.TestCase):
    def test_parent_source_is_injected(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-parent-context-"))
        env = make_env(tmp)
        task = seed_task(env, status="running")
        parent = seed_attempt(env, task, source="// unique parent implementation\n")
        child = seed_attempt(env, task, parent=parent["id"], source="// child implementation\n")
        messages = env.runtime._build_messages(env.tasks.get(task["id"]), child, "system")
        context = messages[1]["content"]
        self.assertIn("## Parent candidate source", context)
        self.assertIn("unique parent implementation", context)
        self.assertIn("minimal diff", context)


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
