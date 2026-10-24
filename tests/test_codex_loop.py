import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ascendforge.codex_loop import run_codex_loop
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
            "ascendforge.codex_loop.CodexAdapter", FakeAdapter
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
            "ascendforge.codex_loop.CodexAdapter", return_value=adapter
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
