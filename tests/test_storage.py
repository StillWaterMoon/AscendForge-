import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ascendforge.experiments import (
    SnapshotError,
    archive_integrity,
    build_task_archive,
    create_snapshot,
    normalize_result,
    read_snapshot,
    update_remote_best,
)
from ascendforge.util import canonical_json

from tests.helpers import make_env, seed_attempt, seed_task


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-store-"))
        self.env = make_env(self.tmp)

    def test_snapshot_roundtrip_and_tamper(self):
        snapshot = create_snapshot(
            self.env.db,
            self.env.settings,
            task_id="task_x",
            attempt_id=None,
            files={"kernel.asc": "// hello\n", "inc.h": "#pragma once\n"},
        )
        bundle = read_snapshot(self.env.db, self.env.settings, snapshot["id"])
        self.assertEqual(bundle["files"]["kernel.asc"], "// hello\n")
        self.assertEqual(len(snapshot["sha256"]), 64)
        path = self.env.settings.data_dir / snapshot["rel_dir"] / "kernel.asc"
        path.write_text("// tampered\n", encoding="utf-8")
        with self.assertRaises(SnapshotError):
            read_snapshot(self.env.db, self.env.settings, snapshot["id"])

    def test_normalization(self):
        passed = normalize_result(
            {"status": "Pass", "result": [{"testcase_status": "Pass", "precision_ratio": 0.999, "time": 2}], "score": 7}
        )
        self.assertEqual(passed["outcome"], "passed")
        self.assertTrue(passed["all_passed"])
        self.assertEqual(passed["testcase_passed"], 1)
        self.assertEqual(passed["time_values"], [2])

        mixed = normalize_result(
            {"status": "Pass", "result": [{"testcase_status": "Pass"}, {"testcase_status": "Fail"}]}
        )
        self.assertEqual(mixed["outcome"], "failed")
        self.assertFalse(mixed["all_passed"])

        running = normalize_result({"status": "Running", "result": []})
        self.assertFalse(running["final"])

        compile_error = normalize_result({"status": "Compile Error", "msg": "bad"})
        self.assertEqual(compile_error["outcome"], "compile_error")

        wrong = normalize_result({"status": "Fail", "result": [{"testcase_status": "Wrong Answer"}]})
        self.assertEqual(wrong["outcome"], "failed")

        unknown = normalize_result({"status": "Some New State"})
        self.assertEqual(unknown["outcome"], "unknown")
        self.assertTrue(unknown["final"])

    def test_best_only_replaced_by_comparable_improvement(self):
        task = seed_task(self.env, config={"metric": "score"})
        attempt1 = seed_attempt(self.env, task, source="// v1")
        config = {"metric": "score"}
        result10 = normalize_result({"status": "Pass", "result": [{"testcase_status": "Pass"}], "score": 10})
        self.env.db.execute(
            "UPDATE attempts SET normalized_json=? WHERE id=?",
            (canonical_json(result10), attempt1["id"]),
        )
        best = update_remote_best(self.env.db, task["id"], attempt1["id"], result10, config)
        self.assertEqual(best["attempt_id"], attempt1["id"])

        attempt2 = seed_attempt(self.env, task, parent=attempt1["id"], source="// v2")
        result8 = normalize_result({"status": "Pass", "result": [{"testcase_status": "Pass"}], "score": 8})
        self.env.db.execute(
            "UPDATE attempts SET normalized_json=? WHERE id=?",
            (canonical_json(result8), attempt2["id"]),
        )
        self.assertIsNone(update_remote_best(self.env.db, task["id"], attempt2["id"], result8, config))
        self.assertEqual(self.env.tasks.get(task["id"])["best_attempt_id"], attempt1["id"])

        attempt3 = seed_attempt(self.env, task, parent=attempt1["id"], source="// v3")
        result12 = normalize_result({"status": "Pass", "result": [{"testcase_status": "Pass"}], "score": 12})
        self.env.db.execute(
            "UPDATE attempts SET normalized_json=? WHERE id=?",
            (canonical_json(result12), attempt3["id"]),
        )
        best = update_remote_best(self.env.db, task["id"], attempt3["id"], result12, config)
        self.assertEqual(best["attempt_id"], attempt3["id"])
        self.assertEqual(self.env.tasks.get(task["id"])["best_attempt_id"], attempt3["id"])

        attempt4 = seed_attempt(self.env, task, parent=attempt3["id"], source="// v4")
        failed = normalize_result({"status": "Compile Error", "msg": "nope"})
        self.assertIsNone(update_remote_best(self.env.db, task["id"], attempt4["id"], failed, config))
        self.assertEqual(self.env.tasks.get(task["id"])["best_attempt_id"], attempt3["id"])

    def test_archive_excludes_credentials_and_verifies(self):
        task = seed_task(self.env)
        attempt = seed_attempt(self.env, task, source="// candidate\n")
        self.env.credentials.put("model", {"api_key": "sk-very-secret-123456", "base_url": "https://x/v1", "model": "m"})
        path = build_task_archive(self.env.db, self.env.settings, task["id"])
        self.assertTrue(path.is_file())
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("SHA256SUMS", names)
            combined = b"".join(zf.read(name) for name in names if not name.endswith("SHA256SUMS"))
        self.assertNotIn(b"sk-very-secret-123456", combined)
        self.assertNotIn(b"api_key", combined.lower())
        integrity = archive_integrity(path)
        self.assertTrue(integrity["ok"], integrity)
        # Snapshot is present under its exact bytes.
        self.assertTrue(any("snapshots/" in name and name.endswith("kernel.asc") for name in names))

    def test_task_archive_only_includes_this_task(self):
        task1 = seed_task(self.env, title="one")
        task2 = seed_task(self.env, title="two")
        seed_attempt(self.env, task1, source="// one")
        seed_attempt(self.env, task2, source="// two")
        path = build_task_archive(self.env.db, self.env.settings, task1["id"])
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(manifest["task"]["id"], task1["id"])
        self.assertTrue(all(item["task_id"] == task1["id"] for item in manifest["attempts"]))


if __name__ == "__main__":
    unittest.main()
