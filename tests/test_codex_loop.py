import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ascendforge.codex_loop import (
    CLI_TOOL_SPECS,
    _codex_thread_name,
    _recover_codex_session_ref,
    run_codex_agent,
    run_codex_loop,
)
from ascendforge.experiments import read_snapshot

from tests.helpers import make_env, seed_attempt, seed_task


class FakeAdapter:
    def __init__(self, **kwargs):
        self.sources = ["int f() { return 2; }\n", "int f() { return 3; }\n"]
        self.calls = 0
        self.resume_calls = 0

    def prepare_workspace(self, root, files, **kwargs):
        workspace = Path(root) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for path, content in files.items():
            (workspace / path).write_text(content, encoding="utf-8")
        return workspace

    def _run(self, workspace, output_file):
        self.calls += 1
        source = self.sources[min(self.calls - 1, len(self.sources) - 1)]
        (workspace / "kernel.asc").write_text(source, encoding="utf-8")
        Path(output_file).write_text("changed kernel", encoding="utf-8")
        return SimpleNamespace(
            session_ref="session-1",
            run_id=f"run-{self.calls}",
            exit_code=0,
            timed_out=False,
            usage={"input_tokens": 10, "output_tokens": 2},
            last_message="changed kernel",
            stderr="",
            error=None,
        )

    def start(self, prompt, *, workspace, output_file, sandbox, model=None):
        return self._run(workspace, output_file)

    def resume(self, session_ref, prompt, *, workspace, output_file, model=None):
        self.resume_calls += 1
        return self._run(workspace, output_file)

    def collect_changes(self, workspace, baseline):
        return {"kernel.asc": (workspace / "kernel.asc").read_text(encoding="utf-8")}

    def close(self):
        pass


class CaptureAgentAdapter:
    def __init__(self, **kwargs):
        self.prompts = []
        self.thread_name = kwargs.get("thread_name")

    def prepare_workspace(self, root, files, **kwargs):
        workspace = Path(root) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def start(self, prompt, *, workspace, output_file, model=None, **kwargs):
        self.prompts.append(prompt)
        Path(output_file).write_text("placeholder", encoding="utf-8")
        return SimpleNamespace(
            session_ref="session-placeholder",
            run_id="run-placeholder",
            exit_code=1,
            timed_out=False,
            usage={},
            last_message="placeholder",
            stderr="",
            error="stop test",
        )

    def close(self):
        pass


class LockedThreadAdapter:
    def __init__(self, **kwargs):
        self.closed = False

    def prepare_workspace(self, root, files, **kwargs):
        workspace = Path(root) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def resume(self, session_ref, prompt, *, workspace, output_file, model=None):
        Path(output_file).write_text("", encoding="utf-8")
        return SimpleNamespace(
            session_ref=session_ref,
            run_id="locked",
            exit_code=2,
            timed_out=False,
            usage={},
            last_message="",
            stderr="",
            error="thread already has an active writer",
            error_code="CODEX_THREAD_LOCKED",
            error_stage="thread_resume",
            error_thread_id=session_ref,
            diagnostics=[{"type": "bridge.transport_error"}],
        )

    def start(self, prompt, *, workspace, output_file, model=None, **kwargs):
        raise AssertionError("must not create a new thread")

    def close(self):
        self.closed = True


class FakeWorker:
    def __init__(self, env):
        self.env = env

    def process_queued_submissions(self):
        self.env.db.execute("UPDATE submissions SET sent_at=sent_at-120 WHERE sent_at IS NOT NULL")
        for row in self.env.db.query("SELECT * FROM submissions WHERE state='queued' ORDER BY created_at"):
            claimed, reservation = self.env.scheduler.claim_submission(row["id"])
            if not claimed:
                return 0
            self.env.scheduler.send_claimed(row["id"])
            self.env.db.execute("UPDATE submissions SET next_poll_at=0 WHERE id=?", (row["id"],))
        return 1


class CodexLoopTests(unittest.TestCase):
    def test_locked_thread_is_reported_without_new_thread(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-locked-"))
        env = make_env(tmp)
        task = seed_task(
            env,
            status="running",
            title="Locked thread",
            config={"execution_mode": "codex", "codex_session_ref": "thread-locked"},
        )
        adapter = LockedThreadAdapter()
        with patch("ascendforge.codex_loop.AppServerAdapter", return_value=adapter):
            result = run_codex_agent(env.settings, task["id"], timeout_sec=10)
        refreshed = env.tasks.get(task["id"])
        self.assertEqual(refreshed["status"], "paused")
        self.assertEqual(refreshed["last_error_code"], "CODEX_THREAD_LOCKED")
        self.assertIn("active writer", refreshed["last_error"])
        self.assertEqual(refreshed["config"]["codex_session_ref"], "thread-locked")
        event = env.db.query_one(
            "SELECT payload_json FROM events WHERE task_id=? AND type='codex_resume_failed'",
            (task["id"],),
        )
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["code"], "CODEX_THREAD_LOCKED")
        self.assertEqual(payload["stage"], "thread_resume")
        self.assertEqual(payload["thread_id"], "thread-locked")
        self.assertEqual(result["session_ref"], "thread-locked")

    def test_codex_loop_saves_real_snapshot_and_waits_for_evaluation(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-loop-"))
        env = make_env(tmp)
        task = seed_task(env, status="running", title="Codex loop")
        baseline = seed_attempt(env, task, source="int f() { return 1; }\n")
        env.db.execute(
            "UPDATE tasks SET best_attempt_id=?, performance_best_attempt_id=? WHERE id=?",
            (baseline["id"], baseline["id"], task["id"]),
        )
        env.platform.default_result = {
            "status": "Pass",
            "result": [{"testcase_status": "Pass", "time": 5.0}],
            "score": 1,
        }
        worker = FakeWorker(env)
        with patch("ascendforge.codex_loop._services", return_value=(env.db, env.tasks, env.scheduler, worker)), patch(
            "ascendforge.codex_loop.AppServerAdapter", FakeAdapter
        ):
            result = run_codex_loop(env.settings, task["id"], rounds=1, keep_running=False)
        self.assertEqual(len(result["history"]), 1)
        attempt = env.db.query_one(
            "SELECT * FROM attempts WHERE task_id=? AND provider='codex-cli' ORDER BY ordinal DESC LIMIT 1",
            (task["id"],),
        )
        self.assertIsNotNone(attempt["snapshot_id"])
        snapshot = read_snapshot(env.db, env.settings, attempt["snapshot_id"])
        self.assertEqual(snapshot["files"]["kernel.asc"], "int f() { return 2; }\n")
        submission = env.db.query_one("SELECT * FROM submissions WHERE attempt_id=?", (attempt["id"],))
        self.assertEqual(submission["state"], "finished")
        self.assertEqual(env.tasks.get(task["id"])["status"], "paused")
        runs = env.db.query("SELECT * FROM cli_runs WHERE task_id=?", (task["id"],))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["backend"], "codex")

    def test_recover_session_ref_from_old_event(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-recover-"))
        env = make_env(tmp)
        task = seed_task(env, status="stopped", config={"execution_mode": "codex"})
        env.db.execute(
            "INSERT INTO events(task_id, attempt_id, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (task["id"], None, "codex_thread_started", '{"thread_id":"old-thread"}', 1.0),
        )
        self.assertEqual(_recover_codex_session_ref(env.db, task["id"]), "old-thread")

    def test_codex_thread_name_uses_problem_name(self):
        self.assertEqual(_codex_thread_name({"problem_title": "AddN"}), "AddN算子优化")
        self.assertEqual(
            _codex_thread_name({"problem_json": {"name": "CopySign"}}),
            "CopySign算子优化",
        )
        self.assertEqual(_codex_thread_name({"title": "ADDN算子优化"}), "ADDN算子优化")

    def test_codex_exposes_exactly_three_platform_tools(self):
        self.assertEqual(
            [spec["name"] for spec in CLI_TOOL_SPECS],
            ["read_problem", "read_official_evaluation", "submit_official"],
        )
        self.assertEqual(
            CLI_TOOL_SPECS[0]["inputSchema"]["required"],
            ["url"],
        )

    def test_codex_agent_sends_placeholder_skill_and_pauses_on_api_error(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-agent-"))
        env = make_env(tmp)
        task = seed_task(env, status="running", title="Codex agent", config={"execution_mode": "codex"})
        adapter = CaptureAgentAdapter()
        worker = FakeWorker(env)
        with patch("ascendforge.codex_loop._services", return_value=(env.db, env.tasks, env.scheduler, worker)), patch(
            "ascendforge.codex_loop.AppServerAdapter", return_value=adapter
        ):
            result = run_codex_agent(env.settings, task["id"], timeout_sec=10)
        self.assertEqual(result["turns"], 1)
        self.assertIn("持续优化算子", adapter.prompts[0])
        self.assertIn("TOOLS.md", adapter.prompts[0])
        self.assertIn("任务启动信息", adapter.prompts[0])
        # API/网络错误即使 exit_code=0 也必须结束并暂停，不能留下 running 空壳。
        task_now = env.tasks.get(task["id"])
        self.assertEqual(task_now["status"], "paused")
        self.assertIn("stop test", task_now.get("last_error") or "")

    def test_codex_loop_resumes_session_for_second_round(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-loop2-"))
        env = make_env(tmp)
        task = seed_task(env, status="running", title="Codex loop 2", config={"min_submit_interval_sec": 60})
        baseline = seed_attempt(env, task, source="int f() { return 1; }\n")
        env.db.execute(
            "UPDATE tasks SET best_attempt_id=?, performance_best_attempt_id=? WHERE id=?",
            (baseline["id"], baseline["id"], task["id"]),
        )
        env.platform.default_result = {
            "status": "Pass",
            "result": [{"testcase_status": "Pass", "time": 5.0}],
            "score": 1,
        }
        adapter = FakeAdapter()
        worker = FakeWorker(env)
        with patch("ascendforge.codex_loop._services", return_value=(env.db, env.tasks, env.scheduler, worker)), patch(
            "ascendforge.codex_loop.AppServerAdapter", return_value=adapter
        ):
            result = run_codex_loop(env.settings, task["id"], rounds=2, keep_running=False)
        self.assertEqual(len(result["history"]), 2)
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(adapter.resume_calls, 1)
        attempts = env.db.query(
            "SELECT * FROM attempts WHERE task_id=? AND provider='codex-cli' ORDER BY ordinal", (task["id"],)
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]["parent_attempt_id"], attempts[0]["id"])
