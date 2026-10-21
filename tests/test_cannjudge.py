import tempfile
import unittest
import zipfile
from pathlib import Path

from ascendforge.platform.base import PlatformError
from ascendforge.platform.cannjudge import CannjudgeAdapter

PROBLEM = {"_id": "p1", "code_template": "npu_kernel_dev"}
TEMPLATE = {
    "files": [
        {"path": "kernel.asc", "content": "// template\n", "editable": True},
        {"path": "main.asc", "content": "// main\n", "editable": False},
        {"path": "CMakeLists.txt", "content": "# build\n", "editable": False},
    ]
}


class CannjudgePayloadTests(unittest.TestCase):
    def setUp(self):
        self.adapter = CannjudgeAdapter("https://cannjudge.cn")

    def test_kernel_payload_exact_files(self):
        payload = self.adapter.build_submission_payload(
            PROBLEM,
            TEMPLATE,
            {"kernel.asc": "// changed\n", "helper.h": "#pragma once\n"},
            user_id="u1",
        )
        self.assertEqual(payload["problemId"], "p1")
        self.assertEqual(payload["userId"], "u1")
        files = {item["path"]: item["content"] for item in payload["files"]}
        self.assertEqual(files["kernel.asc"], "// changed\n")
        self.assertEqual(files["helper.h"], "#pragma once\n")
        self.assertNotIn("main.asc", files)

    def test_missing_editable_rejected(self):
        with self.assertRaises(PlatformError) as ctx:
            self.adapter.build_submission_payload(PROBLEM, TEMPLATE, {"helper.h": "x"}, user_id="u")
        self.assertEqual(ctx.exception.code, "MISSING_EDITABLE_FILES")

    def test_readonly_modification_rejected(self):
        with self.assertRaises(PlatformError) as ctx:
            self.adapter.build_submission_payload(
                PROBLEM,
                TEMPLATE,
                {"kernel.asc": "// k\n", "main.asc": "// hacked\n"},
                user_id="u",
            )
        self.assertEqual(ctx.exception.code, "READONLY_MODIFIED")

    def test_readonly_unchanged_is_dropped(self):
        payload = self.adapter.build_submission_payload(
            PROBLEM,
            TEMPLATE,
            {"kernel.asc": "// k\n", "main.asc": "// main\n"},
            user_id="u",
        )
        self.assertEqual([item["path"] for item in payload["files"]], ["kernel.asc"])

    def test_forbidden_file_rejected(self):
        with self.assertRaises(PlatformError) as ctx:
            self.adapter.build_submission_payload(
                PROBLEM,
                TEMPLATE,
                {"kernel.asc": "// k\n", "judge.asc": "// judge\n"},
                user_id="u",
            )
        self.assertEqual(ctx.exception.code, "FORBIDDEN_FILE")

    def test_extra_subdirectory_rejected(self):
        with self.assertRaises(PlatformError) as ctx:
            self.adapter.build_submission_payload(
                PROBLEM,
                TEMPLATE,
                {"kernel.asc": "// k\n", "sub/helper.h": "x"},
                user_id="u",
            )
        self.assertEqual(ctx.exception.code, "EXTRA_FILE_REJECTED")

    def test_registry_payload(self):
        problem = {"_id": "p2", "code_template": "registry"}
        template = {"files": []}
        files = {
            "op_kernel/add_tiling.h": "// tiling",
            "op_host/add.cpp": "// host",
            "op_kernel/add.cpp": "// kernel",
        }
        payload = self.adapter.build_submission_payload(problem, template, files, user_id="u2")
        self.assertEqual(payload["problemId"], "p2")
        self.assertEqual(payload["tiling_h"], "// tiling")
        self.assertEqual(payload["host_cpp"], "// host")
        self.assertEqual(payload["kernel_cpp"], "// kernel")

    def test_zip_traversal_rejected(self):
        tmp = Path(tempfile.mkdtemp(prefix="af-zip-"))
        archive = tmp / "bad.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.txt", "bad")
        with self.assertRaises(PlatformError):
            CannjudgeAdapter.extract_package(archive, tmp / "out")


if __name__ == "__main__":
    unittest.main()
