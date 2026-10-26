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
)

from tests.helpers import make_env, seed_task


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

    def test_recovery_mode_injects_recovery_skill_and_independent_thread(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-codex-recovery-"))
        env = make_env(tmp)
        task = seed_task(env, status="running", title="Recovery prompt", config={"execution_mode": "codex"})
        adapter = CaptureAgentAdapter()
        worker = FakeWorker(env)

        def make_adapter(**kwargs):
            adapter.thread_name = kwargs.get("thread_name")
            return adapter

        with patch("ascendforge.codex_loop._services", return_value=(env.db, env.tasks, env.scheduler, worker)), patch(
            "ascendforge.codex_loop.AppServerAdapter", side_effect=make_adapter
        ):
            result = run_codex_agent(
                env.settings,
                task["id"],
                timeout_sec=10,
                mode="recovery",
                skill_name="recover-operator-workload",
                skip_preflight=True,
            )
        self.assertEqual(result["turns"], 1)
        self.assertIn("恢复算子输入规格", adapter.prompts[0])
        self.assertIn("第一步必须先调用 read_problem(url)", adapter.prompts[0])
        self.assertIn("不得生成或保存大规模 tensor", adapter.prompts[0])
        self.assertIn("逆向评测数据", adapter.thread_name)
        self.assertEqual(env.tasks.get(task["id"])["status"], "paused")

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
