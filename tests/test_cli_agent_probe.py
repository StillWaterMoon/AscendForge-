import unittest
from pathlib import Path

from ascendforge.cli_agent import probe


class CliProbeTests(unittest.TestCase):
    def test_features_detects_supported_flags(self):
        features = probe._features("Run non-interactively\n--json\nresume a previous session\n--sandbox")
        self.assertTrue(features["json_events"])
        self.assertTrue(features["resume_session"])
        self.assertTrue(features["sandbox_mode"])
        self.assertFalse(features["output_last_message"])

    def test_codex_start_command_keeps_prompt_last(self):
        command = probe.codex_start_command(
            "codex.cmd",
            workspace=Path("D:/work"),
            prompt="do work",
            output_file=Path("D:/work/out.txt"),
            model="gpt-5",
        )
        self.assertEqual(command[0], "codex.cmd")
        self.assertIn("exec", command)
        self.assertIn("--json", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[-1], "do work")

    def test_codex_resume_command_places_output_before_prompt(self):
        command = probe.codex_resume_command(
            "codex.cmd",
            session_ref="session-1",
            prompt="continue",
            output_file=Path("out.txt"),
        )
        self.assertEqual(command[-1], "continue")
        self.assertLess(command.index("-o"), command.index("continue"))

    def test_deepseek_probe_reports_missing_without_credentials(self):
        old = probe._which
        probe._which = lambda *args, **kwargs: None
        try:
            result = probe.probe_deepseek()
        finally:
            probe._which = old
        self.assertFalse(result["supported"])
        self.assertIsNone(result["executable"])
        self.assertIsNone(result["authenticated"])
