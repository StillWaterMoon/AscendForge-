import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ascendforge.util import canonical_json
from ascendforge.workload_recovery import RecoveryError, WorkloadRecoveryService, fingerprint_for_task
from tests.helpers import make_env, seed_attempt, seed_task


class WorkloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-recovery-"))
        self.env = make_env(self.tmp)
        self.task = seed_task(self.env, status="running", title="Recovery task")
        self.service = WorkloadRecoveryService(
            self.env.db, self.env.settings, self.env.tasks, self.env.scheduler
        )

    def _start(self):
        with patch.object(self.service, "launch", return_value={}):
            return self.service.start(self.task["id"])

    def _job_and_hidden(self):
        fp = fingerprint_for_task(self.env.tasks.get(self.task["id"]))
        job = self.service.latest_job(fp["spec_key"])
        hidden = self.env.tasks.get(job["workspace_task_id"])
        return fp, job, hidden

    def _write_spec(self, hidden, *, status="measured", refs=None, numel=6):
        attempt = seed_attempt(self.env, hidden, source="// diagnostic recovery")
        workspace = self.env.settings.data_dir / "cli_workspaces" / hidden["id"] / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        spec = {
            "schema": "ascendforge-workload-spec/1",
            "status": "verified",
            "testcases": [
                {
                    "testcase_id": "case-1",
                    "inputs": [{"name": "x", "dtype": "float16", "shape": [2, 3], "numel": numel}],
                    "outputs": [{"name": "y", "dtype": "float16", "shape": [2, 3]}],
                    "attributes": {"axis": 1},
                    "evidence_status": status,
                    "evidence_refs": refs if refs is not None else [{"attempt_id": attempt["id"]}],
                }
            ],
        }
        (workspace / "specifications.json").write_text(json.dumps(spec), encoding="utf-8")
        (workspace / "evidence.md").write_text("evidence", encoding="utf-8")
        return attempt

    def test_start_creates_hidden_task_and_independent_workspace(self):
        result = self._start()
        self.assertEqual(result["mode"], "started")
        fp, job, hidden = self._job_and_hidden()
        self.assertTrue(hidden["is_recovery"])
        self.assertEqual(hidden["recovery_job_id"], job["id"])
        self.assertNotIn(hidden["id"], [item["id"] for item in self.env.tasks.list()])
        self.assertEqual(hidden["config"]["recovery_mode"], True)
        self.assertIsNone(self.env.tasks.get(self.task["id"]).get("best_attempt_id"))

    def test_import_verified_and_reuse_cache_without_new_job(self):
        self._start()
        fp, job, hidden = self._job_and_hidden()
        attempt = self._write_spec(hidden)
        imported = self.service.import_job(job["id"])
        self.assertEqual(imported["status"], "verified")
        self.env.db.execute(
            "UPDATE workload_recovery_jobs SET status='completed', finished_at=? WHERE id=?",
            (1.0, job["id"]),
        )
        spec = self.service.latest_spec(fp["spec_key"])
        self.assertEqual(spec["status"], "verified")
        self.assertEqual(self.service.spec_public(spec)["testcases"][0]["inputs"][0]["numel"], 6)
        self.env.db.execute("UPDATE attempts SET diagnostic=1 WHERE id=?", (attempt["id"],))
        before = int(self.env.db.scalar("SELECT COUNT(*) FROM workload_recovery_jobs"))
        with patch.object(self.service, "launch") as launch:
            result = self.service.start(self.task["id"])
        self.assertEqual(result["mode"], "cache")
        launch.assert_not_called()
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM workload_recovery_jobs")), before)

    def test_partial_and_measured_without_evidence_are_not_verified(self):
        self._start()
        _fp, job, hidden = self._job_and_hidden()
        self._write_spec(hidden, status="inferred", refs=[])
        imported = self.service.import_job(job["id"])
        self.assertEqual(imported["status"], "partial")
        self.env.db.execute("UPDATE workload_recovery_jobs SET status='partial' WHERE id=?", (job["id"],))
        with patch.object(self.service, "launch", return_value={}):
            result = self.service.start(self.task["id"])
        self.assertEqual(result["mode"], "partial")

    def test_measured_without_valid_refs_is_downgraded(self):
        self._start()
        _fp, job, hidden = self._job_and_hidden()
        self._write_spec(hidden, status="measured", refs=[{"submission_id": "missing"}])
        imported = self.service.import_job(job["id"])
        self.assertEqual(imported["status"], "partial")
        spec = self.service.latest_spec(job["spec_key"])
        self.assertEqual(self.service.spec_public(spec)["testcases"][0]["evidence_status"], "inferred")

    def test_numel_mismatch_is_rejected(self):
        self._start()
        _fp, job, hidden = self._job_and_hidden()
        self._write_spec(hidden, numel=99)
        with self.assertRaises(RecoveryError) as ctx:
            self.service.import_job(job["id"])
        self.assertEqual(ctx.exception.code, "BAD_SPEC")

    def test_testset_change_does_not_reuse_old_spec(self):
        fp1 = fingerprint_for_task(self.task)
        problem = dict(self.task["problem_json"])
        problem["testcases"] = [{"testcase_id": "case-2"}]
        changed = dict(self.task)
        changed["problem_json"] = problem
        fp2 = fingerprint_for_task(changed)
        self.assertNotEqual(fp1["spec_key"], fp2["spec_key"])

    def test_running_job_is_reused_without_launching_another_session(self):
        first = self._start()
        with patch.object(self.service, "launch") as launch:
            second = self.service.start(self.task["id"])
        self.assertEqual(second["mode"], "active")
        self.assertEqual(second["job"]["id"], first["job"]["id"])
        launch.assert_not_called()

    def test_run_job_imports_verified_spec_and_sets_completed(self):
        self._start()
        _fp, job, hidden = self._job_and_hidden()
        self._write_spec(hidden)
        with patch("ascendforge.codex_loop.run_codex_agent", return_value={"turns": 1}):
            result = self.service.run_job(job["id"])
        self.assertEqual(result["status"], "completed")
        refreshed = self.service.get_job(job["id"])
        self.assertEqual(refreshed["status"], "completed")
        spec = self.service.latest_spec(job["spec_key"])
        self.assertEqual(spec["status"], "verified")

    def test_run_job_without_spec_is_failed_not_completed(self):
        self._start()
        _fp, job, _hidden = self._job_and_hidden()
        with patch("ascendforge.codex_loop.run_codex_agent", return_value={"turns": 1}):
            result = self.service.run_job(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.service.get_job(job["id"])["error_code"], "RECOVERY_NO_SPEC")

    def test_second_task_with_same_problem_reuses_verified_spec(self):
        self._start()
        _fp, job, hidden = self._job_and_hidden()
        self._write_spec(hidden)
        with patch("ascendforge.codex_loop.run_codex_agent", return_value={"turns": 1}):
            self.service.run_job(job["id"])
        other = seed_task(self.env, status="running", title="Other optimizer")
        with patch.object(self.service, "launch") as launch:
            result = self.service.start(other["id"])
        self.assertEqual(result["mode"], "cache")
        launch.assert_not_called()

    def test_pause_and_resume_preserve_hidden_task_and_session(self):
        self._start()
        _fp, job, hidden = self._job_and_hidden()
        self.env.db.execute(
            "UPDATE tasks SET config_json=? WHERE id=?",
            (canonical_json({**hidden["config"], "codex_session_ref": "thread-recovery", "codex_runner_active": False}), hidden["id"]),
        )
        paused = self.service.pause(job["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(self.env.tasks.get(hidden["id"])["status"], "paused")
        with patch.object(self.service, "launch", return_value=self.service.get_job(job["id"])):
            resumed = self.service.resume(job["id"])
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(self.env.tasks.get(hidden["id"])["config"].get("codex_session_ref"), "thread-recovery")


if __name__ == "__main__":
    unittest.main()
