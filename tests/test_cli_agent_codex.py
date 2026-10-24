import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ascendforge.cli_agent.codex import CodexAdapter, parse_jsonl


class _Completed:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class CodexAdapterTests(unittest.TestCase):
    def test_parse_jsonl_ignores_noise(self):
        events = parse_jsonl(
            'noise\n{"type":"thread.started","thread_id":"s1"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n'
        )
        self.assertEqual([event.type for event in events], ["thread.started", "turn.completed"])

    def test_workspace_prepare_and_collect_changes(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-ws-"))
        adapter = CodexAdapter("codex.cmd")
        baseline = {"kernel.asc": "int f() { return 1; }\n", "readonly.txt": "x\n"}
        workspace = adapter.prepare_workspace(tmp, baseline, task_meta={"task_id": "t1"})
        self.assertEqual((workspace / "kernel.asc").read_text(), baseline["kernel.asc"])
        self.assertTrue((workspace / "AGENTS.md").exists())
        (workspace / "kernel.asc").write_text("int f() { return 2; }\n")
        changes = adapter.collect_changes(workspace, baseline)
        self.assertEqual(changes, {"kernel.asc": "int f() { return 2; }\n"})

    def test_execute_parses_session_usage_and_last_message(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-run-"))
        output = tmp / "last.txt"
        adapter = CodexAdapter("codex.cmd")
        stdout = (
            '{"type":"thread.started","thread_id":"s1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"DONE"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n'
        )

        def fake_run(*args, **kwargs):
            output.write_text("DONE", encoding="utf-8")
            return _Completed(stdout)

        with patch("ascendforge.cli_agent.codex.subprocess.run", side_effect=fake_run):
            result = adapter.start(
                "do work",
                workspace=tmp,
                output_file=output,
                sandbox="read-only",
            )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.session_ref, "s1")
        self.assertEqual(result.last_message, "DONE")
        self.assertEqual(result.usage["input_tokens"], 10)
        self.assertFalse(result.timed_out)
