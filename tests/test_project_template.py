from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from ascendforge.codex_loop import _template_files
from ascendforge.tasks import TaskError
from tests.helpers import make_env

PROBLEM_URL = "https://cannjudge.cn/hc_cann_codelabs/cann/add"


def make_zip(path: Path, files: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


class ProjectTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = make_env(Path(self.tmp.name))
        self.uploads = self.env.settings.tmp_dir / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_uploaded_project_starts_from_zip_and_strips_single_root(self):
        archive = make_zip(
            self.uploads / "upload_demo.zip",
            {
                "myproject/kernel.asc": "// uploaded kernel\n",
                "myproject/src/helper.h": "// helper\n",
            },
        )
        task = self.env.tasks.create(
            title="Uploaded task",
            problem_url=PROBLEM_URL,
            config={"execution_mode": "codex"},
            project_archive=archive,
        )
        files = {item["path"]: item["content"] for item in task["template"]["files"]}
        self.assertEqual(files["kernel.asc"], "// uploaded kernel\n")
        self.assertEqual(files["src/helper.h"], "// helper\n")
        self.assertEqual(task["config"]["project_source"], "upload")
        self.assertEqual(task["config"]["project_template_files"], 2)
        self.assertTrue(task["config"]["project_template_sha256"])
        self.assertEqual(
            _template_files(task),
            {"kernel.asc": "// uploaded kernel\n", "src/helper.h": "// helper\n"},
        )
        self.assertTrue((self.env.settings.data_dir / task["config"]["project_template_archive"]).is_file())

    def test_no_upload_starts_from_empty_project(self):
        task = self.env.tasks.create(
            title="Empty task",
            problem_url=PROBLEM_URL,
            config={"execution_mode": "codex"},
        )
        self.assertEqual(task["template"]["files"], [])
        self.assertEqual(task["config"]["project_source"], "empty")
        self.assertEqual(task["config"]["project_template_files"], 0)
        self.assertEqual(_template_files(task), {})

    def test_zip_path_traversal_is_rejected(self):
        archive = make_zip(
            self.uploads / "upload_evil.zip",
            {"../evil.asc": "// escape\n"},
        )
        with self.assertRaises(TaskError):
            self.env.tasks.create(
                title="Bad task",
                problem_url=PROBLEM_URL,
                config={"execution_mode": "codex"},
                project_archive=archive,
            )
