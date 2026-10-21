import tempfile
import unittest
from pathlib import Path

from ascendforge.backup import create_backup, restore_backup, verify_backup

from tests.helpers import make_env, seed_attempt, seed_task


class BackupTests(unittest.TestCase):
    def test_backup_verify_restore_and_tamper(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-backup-"))
        env = make_env(tmp)
        task = seed_task(env)
        seed_attempt(env, task, source="// backup candidate\n")
        env.credentials.put("model", {"api_key": "sk-backup-secret", "base_url": "https://x/v1", "model": "m"})

        result = create_backup(env.settings, env.db, include_key=False)
        self.assertGreater(result["files"], 1)
        verification = verify_backup(result["path"])
        self.assertTrue(verification["ok"], verification)
        self.assertFalse(verification["master_key_included"])
        manifest = (Path(result["path"]) / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-backup-secret", manifest)

        restored_dir = tmp / "restored"
        restored = restore_backup(result["path"], restored_dir)
        self.assertGreater(restored["restored_files"], 0)
        self.assertTrue((restored_dir / "ascendforge.db").is_file())
        self.assertTrue((restored_dir / "tasks" / task["id"]).is_dir())

        database = Path(result["path"]) / "ascendforge.db"
        with open(database, "ab") as handle:
            handle.write(b"tamper")
        bad = verify_backup(result["path"])
        self.assertFalse(bad["ok"])

    def test_backup_can_include_key_explicitly(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-backup-key-"))
        env = make_env(tmp)
        result = create_backup(env.settings, env.db, include_key=True)
        self.assertTrue(result["master_key_included"])
        verification = verify_backup(result["path"])
        self.assertTrue(verification["ok"], verification)
        self.assertTrue((Path(result["path"]) / "secrets" / "master.key").is_file())


if __name__ == "__main__":
    unittest.main()
