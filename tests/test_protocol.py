import unittest

from ascendforge.agent.protocol import (
    TOOLS,
    ActionError,
    action_from_text,
    extract_json_object,
    validate_action,
    validate_schema,
)


class ProtocolTests(unittest.TestCase):
    def test_valid_action(self):
        action = validate_action(
            {
                "protocol_version": "1",
                "action_id": "act-1",
                "task_id": "task-1",
                "attempt_id": "att-1",
                "tool_name": "read_problem",
                "arguments": {"include_contents": True},
            },
            task_id="task-1",
            attempt_id="att-1",
            tools=TOOLS,
        )
        self.assertEqual(action.tool_name, "read_problem")
        self.assertTrue(action.arguments["include_contents"])

    def test_unknown_tool_rejected(self):
        with self.assertRaises(ActionError) as ctx:
            validate_action(
                {"tool_name": "run_shell", "arguments": {"cmd": "rm -rf /"}},
                task_id="t",
                attempt_id="a",
                tools=TOOLS,
            )
        self.assertEqual(ctx.exception.code, "UNKNOWN_TOOL")

    def test_bad_schema(self):
        with self.assertRaises(ActionError) as ctx:
            validate_action(
                {"tool_name": "create_candidate", "arguments": {"files": [], "hypothesis": ""}},
                task_id="t",
                attempt_id="a",
                tools=TOOLS,
            )
        self.assertEqual(ctx.exception.code, "BAD_ARGUMENTS")

    def test_task_mismatch(self):
        with self.assertRaises(ActionError) as ctx:
            validate_action(
                {"tool_name": "read_problem", "arguments": {}, "task_id": "other"},
                task_id="t",
                attempt_id="a",
                tools=TOOLS,
            )
        self.assertEqual(ctx.exception.code, "TASK_MISMATCH")

    def test_extract_json_with_fence_and_text(self):
        text = 'Here is the action:\n```json\n{"tool_name":"read_history","arguments":{"limit":3}}\n```\n'
        payload = extract_json_object(text)
        self.assertEqual(payload["tool_name"], "read_history")

    def test_action_from_text(self):
        action = action_from_text(
            '{"protocol_version":"1","tool_name":"query_result","arguments":{}}',
            task_id="t",
            attempt_id="a",
        )
        self.assertEqual(action.tool_name, "query_result")
        self.assertTrue(action.action_id.startswith("auto-"))

    def test_no_action(self):
        with self.assertRaises(ActionError) as ctx:
            action_from_text("I will now optimize the kernel.", task_id="t", attempt_id="a")
        self.assertEqual(ctx.exception.code, "NO_ACTION")

    def test_schema_nested(self):
        schema = TOOLS["create_candidate"]["parameters"]
        errors = validate_schema(
            {"files": [{"path": "kernel.asc", "content": "x"}], "hypothesis": "h"},
            schema,
        )
        self.assertEqual(errors, [])
        errors = validate_schema(
            {"files": [{"path": 1, "content": "x"}], "hypothesis": "h"},
            schema,
        )
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
