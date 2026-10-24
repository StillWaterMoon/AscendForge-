from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ascendforge.mcp.contracts import TOOL_NAMES, ToolArgumentError, validate_arguments
from ascendforge.mcp.functions import MCPToolFunctions
from ascendforge.mcp.server import MCPServer
from ascendforge.mcp.server_eval import ServerEvaluationService
from tests.helpers import make_env, seed_task


class MCPToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = make_env(Path(self.tmp.name))
        self.task = seed_task(self.env, status="running")
        self.workspace = Path(self.tmp.name) / "draft"
        self.workspace.mkdir()
        (self.workspace / "kernel.asc").write_text("// current draft\n", encoding="utf-8")
        self.functions = MCPToolFunctions(
            self.env.settings,
            self.env.db,
            self.task["id"],
            workspace=self.workspace,
            platform=self.env.platform,
            tasks=self.env.tasks,
            scheduler=self.env.scheduler,
            server_eval=ServerEvaluationService(self.env.settings, self.env.db),
            problem_reader=lambda url: {
                "_id": self.task["problem_id"],
                "name": "add",
                "title": "Add",
                "desc": "# Add\n\n" + ("完整题面内容\n" * 300),
                "code_template": "npu_kernel_dev",
                "testcases": [{"testcase_id": "case-1"}],
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_six_tools_are_declared(self):
        self.assertEqual(
            set(TOOL_NAMES),
            {
                "read_problem",
                "read_problem_file",
                "read_server_evaluation",
                "read_official_evaluation",
                "submit_server_test",
                "submit_official",
            },
        )

    def test_read_problem_and_file(self):
        problem = self.functions.call(
            "read_problem", {"url": "https://cannjudge.cn/public/vector/add"}
        )
        self.assertEqual(problem["task_id"], self.task["id"])
        self.assertIn("editable_scope", problem)
        self.assertIn("statement", problem)
        self.assertEqual(problem["statement_format"], "markdown")
        self.assertGreater(problem["statement_length"], 1000)
        self.assertEqual(problem["testcase_count"], 1)
        file = self.functions.call("read_problem_file", {"path": "kernel.asc"})
        self.assertEqual(file["source"], "workspace")
        self.assertEqual(file["content"], "// current draft\n")
        readonly = self.functions.call("read_problem_file", {"path": "main.asc", "source": "template"})
        self.assertIn("read-only", readonly["note"])

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(Exception):
            self.functions.call("read_problem_file", {"path": "../secret.txt"})
        with self.assertRaises(ToolArgumentError):
            validate_arguments("read_problem_file", {"path": "kernel.asc", "extra": 1})

    def test_server_evaluation_id_and_query(self):
        submitted = self.functions.call("submit_server_test", {"request_id": "req-server-1"})
        self.assertTrue(submitted["evaluation_id"])
        self.assertEqual(submitted["status"], "not_configured")
        result = self.functions.call(
            "read_server_evaluation", {"evaluation_id": submitted["evaluation_id"]}
        )
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["source_sha256"], submitted["source_sha256"])
        again = self.functions.call("submit_server_test", {"request_id": "req-server-1"})
        self.assertEqual(again["evaluation_id"], submitted["evaluation_id"])

    def test_stdio_server_handshake_and_call(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        env["ASCENDFORGE_DATA_DIR"] = str(self.env.settings.data_dir)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_problem_file", "arguments": {"path": "kernel.asc"}},
            },
        ]
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ascendforge.mcp.server",
                "--task-id",
                self.task["id"],
                "--workspace",
                str(self.workspace),
            ],
            input="".join(json.dumps(item) + "\n" for item in messages),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "ascendforge")
        self.assertEqual(len(responses[1]["result"]["tools"]), 6)
        self.assertFalse(responses[2]["result"]["isError"])
        payload = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(payload["content"], "// current draft\n")

    def test_server_evaluation_command_runs_and_reports_logs(self):
        import time

        settings = self.env.settings
        (self.workspace / "check.py").write_text("print('server-ok')\n", encoding="utf-8")
        settings.server_eval_enabled = True
        settings.server_eval_command = f"{sys.executable} check.py"
        settings.server_eval_timeout = 30
        service = ServerEvaluationService(settings, self.env.db)
        functions = MCPToolFunctions(
            settings,
            self.env.db,
            self.task["id"],
            workspace=self.workspace,
            platform=self.env.platform,
            tasks=self.env.tasks,
            scheduler=self.env.scheduler,
            server_eval=service,
        )
        submitted = functions.call("submit_server_test", {"request_id": "req-command-1"})
        deadline = time.monotonic() + 10
        result = None
        while time.monotonic() < deadline:
            result = functions.call(
                "read_server_evaluation", {"evaluation_id": submitted["evaluation_id"]}
            )
            if result["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "completed")
        self.assertIn("server-ok", result["logs"]["stdout_tail"])
        self.assertFalse(result["official"])

    def test_official_submission_id_and_idempotency(self):
        first = self.functions.call("submit_official", {"request_id": "req-official-1"})
        self.assertTrue(first["submission_id"])
        self.assertTrue(first["source_sha256"])
        self.assertEqual(first["state"], "queued")
        second = self.functions.call("submit_official", {"request_id": "req-official-1"})
        self.assertEqual(second["submission_id"], first["submission_id"])
        self.assertTrue(second["reused"])
        result = self.functions.call(
            "read_official_evaluation", {"submission_id": first["submission_id"]}
        )
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["source_sha256"], first["source_sha256"])


class MCPProtocolTests(unittest.TestCase):
    def test_initialize_list_and_call(self):
        server = MCPServer()
        initialized = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual(len(listed["result"]["tools"]), 6)
        no_context = server.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_problem"}}
        )
        self.assertTrue(no_context["result"]["isError"])
