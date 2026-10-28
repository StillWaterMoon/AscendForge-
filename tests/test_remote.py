import tempfile
import unittest
from pathlib import Path

from ascendforge.codex_loop import CLI_TOOL_SPECS, REMOTE_TOOL_SPEC
from ascendforge.remote import (
    RemoteBridgeClient,
    RemoteError,
    RemoteStore,
    _safe_task_dir,
    parse_ssh_connect_command,
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

    def test_jump_host_configuration_is_public_without_secret(self):
        public = self.store.save(
            {
                "host": "179.62.58.131",
                "port": 22,
                "username": "root",
                "auth_type": "key",
                "key_path": "C:/Users/test/.ssh/id_ed25519",
                "remote_root": "/tmp/ascendforge",
                "jump_host": "113.47.8.48",
                "jump_port": 2234,
                "jump_username": "jt_test",
                "jump_auth_type": "password",
                "jump_password": "jump-secret",
            }
        )
        self.assertEqual(public["jump_host"], "113.47.8.48")
        self.assertEqual(public["jump_port"], 2234)
        self.assertEqual(public["jump_username"], "jt_test")
        self.assertNotIn("jump_password", public)
        self.assertEqual(self.store.secret()["jump_password"], "jump-secret")

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

    def test_parse_official_ssh_command(self):
        parsed = parse_ssh_connect_command(
            "ssh -J jt_ABC:jump-secret@113.47.8.48:2234 root@179.62.58.131",
            "target-secret",
        )
        self.assertEqual(parsed["host"], "179.62.58.131")
        self.assertEqual(parsed["port"], 22)
        self.assertEqual(parsed["username"], "root")
        self.assertEqual(parsed["password"], "target-secret")
        self.assertEqual(parsed["jump_host"], "113.47.8.48")
        self.assertEqual(parsed["jump_port"], 2234)
        self.assertEqual(parsed["jump_username"], "jt_ABC")
        self.assertEqual(parsed["jump_password"], "jump-secret")
        with self.assertRaises(RemoteError):
            parse_ssh_connect_command("bash root@example.com", "x")
        with self.assertRaises(RemoteError):
            parse_ssh_connect_command("ssh -J bad root@example.com", "")

    def test_task_dir_is_confined(self):
        self.assertTrue(_safe_task_dir("/tmp/af", "task_1").endswith("task_1"))
        with self.assertRaises(RemoteError):
            _safe_task_dir("/tmp/af", "../escape")


if __name__ == "__main__":
    unittest.main()
