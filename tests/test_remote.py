import tempfile
import unittest
from pathlib import Path

from ascendforge.codex_loop import CLI_TOOL_SPECS, REMOTE_TOOL_SPEC
from ascendforge.remote import (
    RemoteBridgeClient,
    RemoteError,
    RemoteStore,
    _safe_task_dir,
)
from ascendforge.security import SecretBox, load_master_key
from tests.helpers import make_env


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-remote-"))
        self.env = make_env(self.tmp)
        box = SecretBox(load_master_key(self.env.settings.key_file, ""))
        self.store = RemoteStore(self.env.db, box)

    def test_password_is_encrypted_and_never_public(self):
        public = self.store.save(
            {
                "host": "192.0.2.10",
                "port": 22,
                "username": "runner",
                "auth_type": "password",
                "password": "super-secret-ssh-password",
                "remote_root": "/tmp/ascendforge",
            }
        )
        self.assertNotIn("password", public)
        self.assertEqual(public["username"], "runner")
        ciphertext = self.env.db.scalar("SELECT ciphertext FROM credentials WHERE kind='remote-server'")
        self.assertNotIn("super-secret-ssh-password", ciphertext)
        self.assertEqual(self.store.secret()["password"], "super-secret-ssh-password")

    def test_key_auth_requires_key_path(self):
        with self.assertRaises(RemoteError):
            self.store.save(
                {
                    "host": "192.0.2.10",
                    "username": "runner",
                    "auth_type": "key",
                    "remote_root": "/tmp/ascendforge",
                }
            )

    def test_workspace_sync_excludes_metadata_and_skill(self):
        workspace = self.tmp / "workspace"
        (workspace / ".ascendforge" / "skills" / "x").mkdir(parents=True)
        (workspace / "kernel.asc").write_text("// kernel", encoding="utf-8")
        (workspace / "TASK.json").write_text("{}", encoding="utf-8")
        (workspace / "specifications.json").write_text("{}", encoding="utf-8")
        (workspace / ".ascendforge" / "skills" / "x" / "SKILL.md").write_text("x", encoding="utf-8")
        files = RemoteBridgeClient._read_workspace(workspace)
        self.assertEqual(files, {"kernel.asc": "// kernel"})

    def test_unavailable_bridge_has_no_remote_tool(self):
        self.assertFalse(RemoteBridgeClient.available(self.env.db))
        with self.assertRaises(RemoteError):
            RemoteBridgeClient(self.env.db).request({"action": "ping"})

    def test_remote_exec_is_separate_from_platform_tools(self):
        self.assertEqual([item["name"] for item in CLI_TOOL_SPECS], [
            "read_problem",
            "read_official_evaluation",
            "submit_official",
        ])
        self.assertEqual(REMOTE_TOOL_SPEC["name"], "remote_exec")
        self.assertEqual(REMOTE_TOOL_SPEC["inputSchema"]["required"], ["command"])

    def test_task_dir_is_confined(self):
        self.assertTrue(_safe_task_dir("/tmp/af", "task_1").endswith("task_1"))
        with self.assertRaises(RemoteError):
            _safe_task_dir("/tmp/af", "../escape")


if __name__ == "__main__":
    unittest.main()
