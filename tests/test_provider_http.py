import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ascendforge.agent.providers import ModelConfig, OpenAICompatibleProvider
from tests.helpers import make_env, seed_task


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8"))
        self.server.requests.append({"body": body, "authorization": self.headers.get("Authorization")})
        if self.server.reject_tools and "tools" in body:
            payload = {"error": {"message": "tools are not supported by this gateway"}}
            self._send(400, payload)
            return
        if not self.server.responses:
            self._send(500, {"error": {"message": "no scripted response"}})
            return
        self._send(200, self.server.responses.pop(0))

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ProviderHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        self.server.requests = []
        self.server.responses = []
        self.server.reject_tools = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _config(self, **overrides):
        payload = {
            "base_url": self.base_url,
            "api_key": "test-key",
            "model": "test-model",
            "supports_tool_calls": False,
            "max_tokens": 256,
            "temperature": 0.1,
            "price_prompt_per_1k": 0.001,
            "price_completion_per_1k": 0.002,
        }
        payload.update(overrides)
        return ModelConfig.from_payload(payload)

    def test_content_action_and_usage_cost(self):
        self.server.responses.append(
            {
                "choices": [{"message": {"role": "assistant", "content": '{"tool_name":"read_problem","arguments":{}}'}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                "model": "test-model",
            }
        )
        provider = OpenAICompatibleProvider(self._config())
        response = provider.complete([{"role": "user", "content": "hello"}], tools=False)
        self.assertIn("read_problem", response.content)
        self.assertEqual(response.prompt_tokens, 1000)
        self.assertEqual(response.completion_tokens, 500)
        cost, known = provider.config.cost(response.prompt_tokens, response.completion_tokens)
        self.assertTrue(known)
        self.assertAlmostEqual(cost, 0.002, places=6)
        self.assertEqual(self.server.requests[0]["authorization"], "Bearer test-key")

    def test_native_tool_call(self):
        self.server.responses.append(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "read_problem", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        config = self._config(supports_tool_calls=True)
        response = OpenAICompatibleProvider(config).complete([{"role": "user", "content": "hello"}], tools=True)
        self.assertEqual(response.tool_calls[0]["name"], "read_problem")
        self.assertEqual(response.tool_calls[0]["arguments"], "{}")

    def test_tools_rejected_falls_back_without_tools(self):
        self.server.reject_tools = True
        self.server.responses.append(
            {
                "choices": [{"message": {"content": '{"tool_name":"read_history","arguments":{}}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        config = self._config(supports_tool_calls=True)
        response = OpenAICompatibleProvider(config).complete([{"role": "user", "content": "hello"}], tools=True)
        self.assertTrue(response.tool_fallback)
        self.assertIn("read_history", response.content)
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("tools", self.server.requests[0]["body"])
        self.assertNotIn("tools", self.server.requests[1]["body"])

    def test_runtime_uses_http_provider_to_create_and_queue(self):
        self.server.responses.extend(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "protocol_version": "1",
                                        "tool_name": "create_candidate",
                                        "arguments": {
                                            "files": [{"path": "kernel.asc", "content": "// http candidate\n"}],
                                            "hypothesis": "http provider baseline",
                                        },
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                },
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"protocol_version": "1", "tool_name": "request_submission", "arguments": {}}
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                },
            ]
        )
        provider = OpenAICompatibleProvider(self._config())
        env = make_env(Path(tempfile.mkdtemp(prefix="af-http-runtime-")), provider=provider)
        task = seed_task(env, status="running", config={"min_submit_interval_sec": 60})
        self.assertEqual(env.runtime.step(task["id"]), "continue")
        self.assertEqual(env.runtime.step(task["id"]), "waiting")
        submission = env.db.query_one("SELECT * FROM submissions WHERE task_id=?", (task["id"],))
        self.assertIsNotNone(submission)
        self.assertEqual(submission["state"], "queued")
        attempt = env.db.query_one("SELECT * FROM attempts WHERE id=?", (submission["attempt_id"],))
        self.assertIsNotNone(attempt["snapshot_id"])
        self.assertGreaterEqual(len(self.server.requests), 2)


if __name__ == "__main__":
    unittest.main()
