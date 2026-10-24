"""Fixed-test server evaluation backend.

The backend is deliberately command-based.  A deployment that has an Ascend
NPU worker configures a command such as ``bash run.sh npu``; this module runs
that command in a disposable copy of the submitted snapshot and records the
real exit code/logs.  If the backend is not configured it returns
``not_configured`` instead of inventing a device result.

The task's own ``run.sh`` is the fixed test entry: it compiles, generates the
fixed test data, runs the kernel and verifies the reference output.  This is a
server test, not an official CANNJudge result.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from ..experiments import create_snapshot
from ..util import json_dumps, json_loads, new_id, now

LOG_LIMIT = 24000


class ServerEvaluationError(RuntimeError):
    pass


def _tail(value: str, limit: int = LOG_LIMIT) -> str:
    value = value or ""
    return value[-limit:]


def _load_row(row: dict) -> dict:
    config = json_loads(row.get("config_json"), {})
    result = json_loads(row.get("result_json"), {})
    return {
        "evaluation_id": row["id"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "snapshot_id": config.get("snapshot_id"),
        "source_sha256": config.get("source_sha256"),
        "status": result.get("status") or "queued",
        "coverage": result.get("coverage"),
        "compile": result.get("compile"),
        "correctness": result.get("correctness"),
        "timing": result.get("timing"),
        "logs": result.get("logs"),
        "error": result.get("error"),
        "official": False,
        "created_at": row.get("created_at"),
    }


class ServerEvaluationService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def _find_by_request(self, task_id: str, request_id: str | None) -> dict | None:
        if not request_id:
            return None
        return self.db.query_one(
            "SELECT * FROM evaluations WHERE task_id=? AND kind='server-test' AND eval_key=? "
            "ORDER BY created_at ASC LIMIT 1",
            (task_id, request_id),
        )

    def read(self, evaluation_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM evaluations WHERE id=? AND kind='server-test'", (evaluation_id,))
        if not row:
            raise ServerEvaluationError("evaluation not found")
        return _load_row(row)

    def submit(self, task_id: str, files: dict[str, str], *, request_id: str | None = None) -> dict:
        existing = self._find_by_request(task_id, request_id)
        if existing:
            return _load_row(existing)
        if not files:
            raise ServerEvaluationError("no source files to evaluate")
        snapshot = create_snapshot(
            self.db,
            self.settings,
            task_id=task_id,
            attempt_id=None,
            files=files,
            kind="server-test",
        )
        evaluation_id = new_id("eval")
        coverage = "fixed task test suite: run.sh + generated/verified test data"
        initial = {
            "status": "not_configured" if not self.settings.server_eval_enabled else "queued",
            "coverage": coverage,
            "source_sha256": snapshot["sha256"],
            "snapshot_id": snapshot["id"],
            "compile": None,
            "correctness": None,
            "timing": None,
            "logs": None,
            "error": None if self.settings.server_eval_enabled else (
                "server evaluation backend is disabled; set ASCENDFORGE_SERVER_EVAL_ENABLED=1 "
                "and ASCENDFORGE_SERVER_EVAL_COMMAND"
            ),
        }
        config = {
            "snapshot_id": snapshot["id"],
            "source_sha256": snapshot["sha256"],
            "file_count": len(files),
            "command": self.settings.server_eval_command or "bash run.sh npu",
            "coverage": coverage,
            "request_id": request_id,
        }
        self.db.execute(
            "INSERT INTO evaluations(id, task_id, kind, config_json, environment_json, result_json, "
            "eval_key, created_at) VALUES(?, ?, 'server-test', ?, ?, ?, ?, ?)",
            (
                evaluation_id,
                task_id,
                json_dumps(config),
                json_dumps({"backend": "configured_command", "has_npu": None}),
                json_dumps(initial),
                request_id,
                now(),
            ),
        )
        if self.settings.server_eval_enabled:
            thread = threading.Thread(
                target=self._run,
                args=(evaluation_id, files, config),
                name=f"server-eval-{evaluation_id}",
                daemon=True,
            )
            thread.start()
        return self.read(evaluation_id)

    def _update(self, evaluation_id: str, payload: dict) -> None:
        self.db.execute(
            "UPDATE evaluations SET result_json=? WHERE id=?",
            (json_dumps(payload), evaluation_id),
        )

    def _run(self, evaluation_id: str, files: dict[str, str], config: dict) -> None:
        started = time.monotonic()
        result: dict[str, Any] = {
            "status": "running",
            "coverage": config.get("coverage"),
            "source_sha256": config.get("source_sha256"),
            "snapshot_id": config.get("snapshot_id"),
            "compile": None,
            "correctness": None,
            "timing": None,
            "logs": None,
            "error": None,
        }
        self._update(evaluation_id, result)
        workspace: Path | None = None
        try:
            self.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix=f"mcp-eval-{evaluation_id}-", dir=str(self.settings.tmp_dir)))
            for raw_path, content in files.items():
                target = workspace / raw_path
                # create_snapshot already validated paths, but keep the final
                # write check local as well.
                if workspace.resolve() not in target.resolve().parents:
                    raise ServerEvaluationError("source path escapes evaluation workspace")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            command = shlex.split(
                config.get("command") or "bash run.sh npu",
                posix=(os.name != "nt"),
            )
            env = {
                key: os.environ[key]
                for key in (
                    "PATH",
                    "LD_LIBRARY_PATH",
                    "ASCEND_HOME_PATH",
                    "ASCEND_TOOLKIT_HOME",
                    "DEVICE_ID",
                    "NPU_ARCH",
                    "PYTHON",
                    "BUILD_JOBS",
                )
                if key in os.environ
            }
            env.update({"HOME": str(workspace), "TMPDIR": str(workspace), "TEMP": str(workspace)})
            completed = subprocess.run(
                command,
                cwd=str(workspace),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(30, int(self.settings.server_eval_timeout)),
                shell=False,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            ok = completed.returncode == 0
            result.update(
                {
                    "status": "completed" if ok else "failed",
                    "compile": {
                        "ok": ok,
                        "exit_code": completed.returncode,
                        "note": "compile/build/run all come from the configured fixed-test command",
                    },
                    "correctness": {
                        "ok": ok,
                        "note": "fixed task run.sh includes sample.py generate + verify; "
                        "this is server-test coverage, not official OJ coverage",
                    },
                    "timing": {
                        "duration_ms": duration_ms,
                        "method": "process wall clock; not a per-kernel microbenchmark",
                    },
                    "logs": {
                        "stdout_tail": _tail(completed.stdout),
                        "stderr_tail": _tail(completed.stderr),
                        "truncated": len(completed.stdout or "") > LOG_LIMIT
                        or len(completed.stderr or "") > LOG_LIMIT,
                    },
                    "npu_required": True,
                    "official": False,
                }
            )
        except subprocess.TimeoutExpired as exc:
            result.update(
                {
                    "status": "failed",
                    "error": "server evaluation timed out",
                    "logs": {
                        "stdout_tail": _tail((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
                        "stderr_tail": _tail((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
                    },
                }
            )
        except Exception as exc:
            result.update({"status": "failed", "error": str(exc)})
        finally:
            if workspace is not None:
                import shutil

                shutil.rmtree(workspace, ignore_errors=True)
        self._update(evaluation_id, result)
