import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from ascendforge.web import App, Server

from ascendforge.experiments import normalize_result, update_remote_best
from ascendforge.util import canonical_json
from tests.helpers import FakePlatform, make_env, seed_attempt, seed_task


class HttpClient:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, body=None, csrf=None, headers=None):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if csrf:
            hdrs["X-CSRF-Token"] = csrf
        request = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            response = self.opener.open(request, timeout=30)
            raw = response.read()
            status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {"raw": raw}
        return status, payload


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="af-web-"))
        cls.env = make_env(cls.tmp)
        cls.task = seed_task(cls.env, title="Web task")
        cls.attempt = seed_attempt(cls.env, cls.task, source="// web best candidate\n")
        normalized = normalize_result({"status": "Pass", "result": [{"testcase_status": "Pass"}], "score": 9})
        cls.env.db.execute(
            "UPDATE attempts SET status='finished', normalized_json=?, result_json=? WHERE id=?",
            (canonical_json(normalized), canonical_json({"status": "Pass"}), cls.attempt["id"]),
        )
        update_remote_best(cls.env.db, cls.task["id"], cls.attempt["id"], normalized, {"metric": "score"})
        cls.app = App(cls.env.settings)
        cls.app.credentials.put(
            "cannjudge",
            {
                "user_id": "judge-user-1",
                "account": "tester@example.com",
                "cookies": [
                    {
                        "name": "session",
                        "value": "fake",
                        "domain": ".cannjudge.cn",
                        "path": "/",
                        "secure": True,
                        "expires": None,
                    }
                ],
                "base_url": cls.env.settings.cannjudge_base_url,
                "logged_in_at": 1,
            },
        )
        cls.server = Server(("127.0.0.1", 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.client = HttpClient(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_00_index_and_static_assets(self):
        with urllib.request.urlopen(self.base + "/", timeout=20) as response:
            html = response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b"AscendForge", html)
        with urllib.request.urlopen(self.base + "/static/app.js", timeout=20) as response:
            js = response.read()
        self.assertIn(b"renderDashboard", js)
        with urllib.request.urlopen(self.base + "/static/style.css", timeout=20) as response:
            css = response.read()
        self.assertIn(b"--accent", css)

    def test_01_health_and_setup_flow(self):
        status, payload = self.client.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        status, payload = self.client.request("GET", "/api/setup/status")
        self.assertTrue(payload["needs_setup"])

        status, payload = self.client.request("POST", "/api/setup", {"token": "wrong", "username": "admin", "password": "long-enough-password"})
        self.assertEqual(status, 403)

        status, payload = self.client.request(
            "POST",
            "/api/setup",
            {"token": self.env.settings.setup_token, "username": "admin", "password": "long-enough-password"},
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["authenticated"])
        type(self).csrf = payload["csrf_token"]
        self.assertIsNotNone(type(self).csrf)

        status, payload = self.client.request("GET", "/api/auth/me")
        self.assertTrue(payload["authenticated"])

    def test_02_csrf_and_task_control(self):
        status, payload = self.client.request("POST", f"/api/tasks/{self.task['id']}/start", {})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "CSRF")

        status, payload = self.client.request(
            "POST",
            f"/api/tasks/{self.task['id']}/start",
            {},
            csrf=self.csrf,
            headers={"X-Request-Id": "req-start-123456"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["task"]["status"], "running")

        # Replaying the same request id must not execute again (cached response).
        status, payload = self.client.request(
            "POST",
            f"/api/tasks/{self.task['id']}/start",
            {},
            csrf=self.csrf,
            headers={"X-Request-Id": "req-start-123456"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["task"]["status"], "running")

    def test_03_model_settings_masked(self):
        status, payload = self.client.request(
            "PUT",
            "/api/settings/model",
            {
                "base_url": "https://relay.example.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-test-secret-9999",
                "supports_tool_calls": False,
                "max_tokens": 1024,
            },
            csrf=self.csrf,
        )
        self.assertEqual(status, 200, payload)
        self.assertNotIn("sk-test-secret-9999", json.dumps(payload))
        status, payload = self.client.request("GET", "/api/settings/model")
        self.assertEqual(status, 200)
        self.assertNotIn("sk-test-secret-9999", json.dumps(payload))
        self.assertIn("api_key_masked", payload["model"])
        stored = self.app.credentials.get("model")
        self.assertEqual(stored["api_key"], "sk-test-secret-9999")

    def test_04_create_task_via_api_uses_fake_platform(self):
        fake = FakePlatform()

        def resolve(url, task_dir):
            return fake.resolve_and_download(url, task_dir)

        self.app.platform.resolve_and_download = resolve
        status, payload = self.client.request(
            "POST",
            "/api/tasks",
            {
                "title": "API created",
                "problem_url": "https://cannjudge.cn/hc_cann_codelabs/cann/add",
                "config": {"max_submissions_per_task": 5},
            },
            csrf=self.csrf,
        )
        self.assertEqual(status, 200, payload)
        task = payload["task"]
        self.assertEqual(task["title"], "API created")
        self.assertTrue(task["template"]["files"])
        self.assertEqual(task["config"]["max_submissions_per_task"], 5)

        status, payload = self.client.request("GET", f"/api/tasks/{task['id']}")
        self.assertEqual(status, 200)

        status, payload = self.client.request(
            "POST",
            f"/api/tasks/{task['id']}/strategy",
            {"strategy_prompt": "先覆盖 dtype，再优化分块"},
            csrf=self.csrf,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["task"]["config"]["strategy_prompt"], "先覆盖 dtype，再优化分块")

        status, payload = self.client.request(
            "POST",
            f"/api/tasks/{task['id']}/budget",
            {"max_submissions_per_task": 7, "max_submissions_per_day": 4, "max_attempts": 9},
            csrf=self.csrf,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["task"]["config"]["max_submissions_per_task"], 7)
        self.assertEqual(payload["task"]["config"]["max_submissions_per_day"], 4)
        self.assertEqual(payload["task"]["config"]["max_attempts"], 9)

        status, payload = self.client.request(
            "POST",
            f"/api/tasks/{task['id']}/baseline-submit",
            {"confirm": True},
            csrf=self.csrf,
        )
        self.assertEqual(status, 200, payload)
        attempts = payload["task"]["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["provider"], "manual")
        self.assertEqual(payload["task"]["submissions"][0]["state"], "queued")

        # Simulate the baseline finishing, then continue the same task with the
        # Agent so it retains the baseline history.
        self.env.db.execute(
            "UPDATE tasks SET status='completed', stop_reason='manual_baseline_finished' WHERE id=?",
            (task["id"],),
        )
        self.env.db.execute("UPDATE attempts SET status='finished' WHERE id=?", (attempts[0]["id"],))
        status, payload = self.client.request(
            "POST",
            f"/api/tasks/{task['id']}/continue-optimization",
            {},
            csrf=self.csrf,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["task"]["status"], "running")
        self.assertFalse(payload["task"]["config"]["manual_only"])

    def _download(self, path):
        request = urllib.request.Request(
            self.base + path,
            headers={"Cookie": "; ".join(f"{c.name}={c.value}" for c in self.client.jar)},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def test_05_task_archive_download(self):
        data = self._download(f"/api/tasks/{self.task['id']}/archive")
        self.assertTrue(data.startswith(b"PK"))
        import io

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("manifest.json", zf.namelist())
            self.assertIn("SHA256SUMS", zf.namelist())

        best = self._download(f"/api/tasks/{self.task['id']}/download/best")
        self.assertTrue(best.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(best)) as zf:
            self.assertIn("kernel.asc", zf.namelist())

        attempt = self._download(f"/api/tasks/{self.task['id']}/attempts/{self.attempt['id']}/download")
        self.assertTrue(attempt.startswith(b"PK"))

    def test_06_logout(self):
        status, payload = self.client.request("POST", "/api/auth/logout", {}, csrf=self.csrf)
        self.assertEqual(status, 200)
        status, payload = self.client.request("GET", "/api/auth/me")
        self.assertFalse(payload["authenticated"])


if __name__ == "__main__":
    unittest.main()
