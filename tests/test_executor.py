import os
import sys
import tempfile
import unittest
from pathlib import Path

from ascendforge.execution.executor import LocalExecutor


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-exec-"))
        self.executor = LocalExecutor(self.tmp)

    def test_runs_in_scrubbed_environment(self):
        previous = os.environ.get("ASCENDFORGE_SECRET_KEY")
        os.environ["ASCENDFORGE_SECRET_KEY"] = "should-not-leak"
        try:
            result = self.executor.run(
                {"main.py": "import os\nprint(os.environ.get('ASCENDFORGE_SECRET_KEY', 'MISSING'))\n"},
                [sys.executable, "main.py"],
                timeout=10,
                memory_mb=512,
            )
        finally:
            if previous is None:
                os.environ.pop("ASCENDFORGE_SECRET_KEY", None)
            else:
                os.environ["ASCENDFORGE_SECRET_KEY"] = previous
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout.strip(), "MISSING")
        self.assertFalse(result.network_enforced)

    def test_timeout_kills_process(self):
        result = self.executor.run(
            {"main.py": "import time\ntime.sleep(10)\n"},
            [sys.executable, "main.py"],
            timeout=1,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_output_is_capped(self):
        result = self.executor.run(
            {"main.py": "print('x' * 400000)\n"},
            [sys.executable, "main.py"],
            timeout=10,
        )
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.stdout), 256 * 1024)
        self.assertTrue(result.truncated)

    def test_artifacts_collected(self):
        result = self.executor.run(
            {"main.py": "import os\nos.makedirs('out', exist_ok=True)\nopen('out/result.txt','w').write('ok')\n"},
            [sys.executable, "main.py"],
            timeout=10,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.artifacts["out/result.txt"], b"ok")

    def test_rejects_path_traversal(self):
        with self.assertRaises(Exception):
            self.executor.run({"../escape": "x"}, [sys.executable, "-c", "pass"], timeout=5)


if __name__ == "__main__":
    unittest.main()
