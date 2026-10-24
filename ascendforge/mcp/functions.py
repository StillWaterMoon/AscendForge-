"""The six function-call implementations behind the MCP server."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from ..experiments import (
    attach_snapshot,
    create_attempt,
    get_attempt,
    read_snapshot,
    update_attempt,
)
from ..platform.service import PlatformService
from ..scheduler import Scheduler
from ..security import CredentialStore, SecretBox, load_master_key
from ..tasks import TaskService
from ..util import json_dumps, json_loads, new_id, now, safe_relpath
from .contracts import TOOL_NAMES, ToolArgumentError, validate_arguments
from .server_eval import ServerEvaluationError, ServerEvaluationService

SOURCE_SUFFIXES = {".asc", ".h", ".hpp", ".cpp", ".cc", ".py", ".sh", ".txt", ".json", ".cmake"}
META_NAMES = {"TASK.json", "AGENTS.md", "codex-tools.json", "codex-prompt.txt"}
MAX_SOURCE_FILES = 64
MAX_SOURCE_BYTES = 2 * 1024 * 1024


class MCPToolError(RuntimeError):
    pass


class MCPToolFunctions:
    """Task-scoped tool implementation.

    The server is started with a task id and the CLI's draft directory.  Tools
    never accept an arbitrary filesystem path and never return credentials.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        task_id: str,
        *,
        workspace: str | Path | None = None,
        platform: Any | None = None,
        tasks: TaskService | None = None,
        scheduler: Scheduler | None = None,
        server_eval: ServerEvaluationService | None = None,
    ):
        self.settings = settings
        self.db = db
        self.task_id = task_id
        self.workspace = Path(workspace) if workspace else settings.data_dir / "cli_workspaces" / task_id / "workspace"
        if platform is None:
            box = SecretBox(load_master_key(settings.key_file, ""))
            platform = PlatformService(db, settings, CredentialStore(db, box))
        self.platform = platform
        self.tasks = tasks or TaskService(db, settings, platform)
        self.scheduler = scheduler or Scheduler(db, settings, platform, self.tasks)
        self.server_eval = server_eval or ServerEvaluationService(settings, db)
        self._task = self.tasks.get(task_id)
        self._problem = json_loads(self._task.get("problem_json"), {})
        self._template = json_loads(self._task.get("template_json"), {})
        self._template_files = {
            str(item.get("path")): item for item in (self._template.get("files") or []) if item.get("path")
        }

    # ------------------------------------------------------------------
    # Source collection
    # ------------------------------------------------------------------
    def _latest_snapshot_attempt(self) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM attempts WHERE task_id=? AND snapshot_id IS NOT NULL "
            "ORDER BY ordinal DESC LIMIT 1",
            (self.task_id,),
        )

    def _workspace_source_files(self) -> dict[str, str]:
        if not self.workspace.is_dir():
            return {}
        files: dict[str, str] = {}
        total = 0
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name in META_NAMES:
                continue
            rel = safe_relpath(path.relative_to(self.workspace).as_posix())
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            encoded = content.encode("utf-8")
            total += len(encoded)
            if len(files) >= MAX_SOURCE_FILES or total > MAX_SOURCE_BYTES:
                raise MCPToolError("workspace source exceeds MCP snapshot limits")
            files[rel] = content
        return files

    def _current_source_files(self) -> dict[str, str]:
        files = self._workspace_source_files()
        if files:
            return files
        latest = self._latest_snapshot_attempt()
        if latest and latest.get("snapshot_id"):
            return read_snapshot(self.db, self.settings, latest["snapshot_id"])["files"]
        return {
            path: str(item.get("content") or "")
            for path, item in self._template_files.items()
            if item.get("editable") and str(item.get("content") or "")
        }

    def _source_for_read(self, path: str, source: str, attempt_id: str | None) -> tuple[str, str, str | None]:
        rel = safe_relpath(path)
        if rel not in self._template_files and not (self.workspace / rel).is_file():
            raise MCPToolError("file is not part of this task template or workspace")
        if source == "template":
            item = self._template_files.get(rel)
            if not item:
                raise MCPToolError("file is not in the task template")
            return str(item.get("content") or ""), "template", None
        if source == "workspace":
            target = self.workspace / rel
            if not target.is_file():
                raise MCPToolError("file is not in the current workspace")
            return target.read_text(encoding="utf-8", errors="replace"), "workspace", None
        attempt = get_attempt(self.db, attempt_id) if attempt_id else self._latest_snapshot_attempt()
        if not attempt or attempt.get("task_id") != self.task_id or not attempt.get("snapshot_id"):
            raise MCPToolError("no source snapshot is available")
        files = read_snapshot(self.db, self.settings, attempt["snapshot_id"])["files"]
        if rel not in files:
            raise MCPToolError("file is not in the selected snapshot")
        return files[rel], "snapshot", attempt["id"]

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------
    def read_problem(self, arguments: dict) -> dict:
        include_statement = bool(arguments.get("include_statement", True))
        editable = []
        readonly = []
        for path, item in sorted(self._template_files.items()):
            entry = {
                "path": path,
                "editable": bool(item.get("editable")),
                "bytes": len(str(item.get("content") or "").encode("utf-8")),
            }
            (editable if entry["editable"] else readonly).append(entry)
        latest = self._latest_snapshot_attempt()
        result = {
            "task_id": self.task_id,
            "title": self._task.get("title"),
            "problem_id": self._problem.get("_id") or self._task.get("problem_id"),
            "problem_name": self._problem.get("name") or self._task.get("problem_title"),
            "problem_url": self._task.get("problem_url"),
            "code_template": self._problem.get("code_template"),
            "dtypes": self._problem.get("dtypes") or self._problem.get("dtype"),
            "interface": self._problem.get("interface") or self._problem.get("function"),
            "precision": self._problem.get("precision") or self._problem.get("tolerance"),
            "constraints": self._problem.get("constraints"),
            "testcase_count": len(self._problem.get("testcases") or []),
            "file_directory": str(self.workspace),
            "editable_scope": editable,
            "readonly_files": readonly,
            "template_file_count": len(self._template_files),
            "latest_candidate": latest["id"] if latest else None,
            "latest_candidate_snapshot": latest.get("snapshot_id") if latest else None,
            "statement_included": include_statement,
        }
        if include_statement:
            result["statement"] = self._problem.get("description") or self._problem.get("desc") or ""
        return result

    def read_problem_file(self, arguments: dict) -> dict:
        path = str(arguments["path"])
        source = str(arguments.get("source") or "auto")
        if source == "auto":
            if (self.workspace / safe_relpath(path)).is_file():
                source = "workspace"
            elif self._latest_snapshot_attempt():
                source = "snapshot"
            else:
                source = "template"
        content, actual_source, attempt_id = self._source_for_read(
            path, source, arguments.get("attempt_id")
        )
        encoded = content.encode("utf-8")
        if len(encoded) > 512 * 1024:
            raise MCPToolError("file exceeds 512 KiB MCP read limit")
        item = self._template_files.get(safe_relpath(path), {})
        return {
            "path": safe_relpath(path),
            "source": actual_source,
            "attempt_id": attempt_id,
            "editable": bool(item.get("editable")),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "content": content,
            "note": "read-only file" if not item.get("editable") else "editable source file",
        }

    # ------------------------------------------------------------------
    # Evaluation reads
    # ------------------------------------------------------------------
    def read_server_evaluation(self, arguments: dict) -> dict:
        try:
            result = self.server_eval.read(str(arguments["evaluation_id"]))
        except ServerEvaluationError as exc:
            raise MCPToolError(str(exc)) from exc
        if not arguments.get("include_logs", True):
            result = {**result, "logs": None}
        return result

    def read_official_evaluation(self, arguments: dict) -> dict:
        row = self.db.query_one(
            "SELECT * FROM submissions WHERE id=? AND task_id=?",
            (str(arguments["submission_id"]), self.task_id),
        )
        if not row:
            raise MCPToolError("submission not found for this task")
        attempt = get_attempt(self.db, row.get("attempt_id") or "") if row.get("attempt_id") else None
        snapshot = (
            self.db.query_one("SELECT sha256 FROM snapshots WHERE id=?", (row["snapshot_id"],))
            if row.get("snapshot_id")
            else None
        )
        normalized = json_loads(attempt.get("normalized_json"), {}) if attempt else {}
        result = {
            "submission_id": row["id"],
            "attempt_id": row.get("attempt_id"),
            "snapshot_id": row.get("snapshot_id"),
            "source_sha256": snapshot["sha256"] if snapshot else row.get("request_hash"),
            "state": row["state"],
            "remote_id": row.get("remote_id"),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "sent_at": row.get("sent_at"),
            "poll_count": row.get("poll_count"),
            "normalized": normalized,
            "compile": normalized.get("compile"),
            "correctness": normalized.get("correctness"),
            "performance": normalized.get("performance"),
            "score": normalized.get("score"),
            "testcases": normalized.get("testcases") or normalized.get("result") or [],
            "official": True,
        }
        if arguments.get("include_raw"):
            result["raw"] = json_loads(row.get("response_json"), {})
        return result

    # ------------------------------------------------------------------
    # Submissions
    # ------------------------------------------------------------------
    def submit_server_test(self, arguments: dict) -> dict:
        files = self._current_source_files()
        try:
            result = self.server_eval.submit(
                self.task_id, files, request_id=arguments.get("request_id")
            )
        except ServerEvaluationError as exc:
            raise MCPToolError(str(exc)) from exc
        return {
            "evaluation_id": result["evaluation_id"],
            "status": result["status"],
            "source_sha256": result["source_sha256"],
            "snapshot_id": result["snapshot_id"],
            "coverage": result["coverage"],
            "submitted": True,
            "note": "accepted only; query read_server_evaluation for the result",
        }

    def submit_official(self, arguments: dict) -> dict:
        task = self.tasks.get(self.task_id)
        if task["status"] != "running":
            raise MCPToolError(f"task is {task['status']}; task must be running to submit")
        request_id = arguments.get("request_id")
        if request_id:
            existing = self.db.query_one(
                "SELECT * FROM submissions WHERE task_id=? AND eval_key=? ORDER BY created_at ASC LIMIT 1",
                (self.task_id, request_id),
            )
            if existing:
                return self._submission_ack(existing, reused=True)
        parent_id = arguments.get("parent_attempt_id") or self._parent_attempt_id()
        files = self._current_source_files()
        if not files:
            raise MCPToolError("no source files are available to submit")
        attempt = create_attempt(
            self.db,
            self.task_id,
            parent_attempt_id=parent_id,
            provider="mcp",
            model="codex-session",
            prompt_version="mcp-v1",
        )
        update_attempt(self.db, attempt["id"], hypothesis="MCP/Codex candidate")
        snapshot = attach_snapshot(
            self.db, self.settings, task_id=self.task_id, attempt_id=attempt["id"], files=files
        )
        submission = self.scheduler.request_submission(self.task_id, attempt["id"])
        if request_id:
            self.db.execute(
                "UPDATE submissions SET eval_key=?, updated_at=? WHERE id=?",
                (request_id, now(), submission["id"]),
            )
            submission = self.db.query_one("SELECT * FROM submissions WHERE id=?", (submission["id"],))
        return self._submission_ack(submission, reused=False, attempt=attempt, snapshot=snapshot)

    def _parent_attempt_id(self) -> str | None:
        latest = self.db.query_one(
            "SELECT id FROM attempts WHERE task_id=? AND snapshot_id IS NOT NULL "
            "ORDER BY ordinal DESC LIMIT 1",
            (self.task_id,),
        )
        return latest["id"] if latest else None

    def _submission_ack(
        self,
        submission: dict,
        *,
        reused: bool,
        attempt: dict | None = None,
        snapshot: dict | None = None,
    ) -> dict:
        snapshot_row = self.db.query_one(
            "SELECT sha256 FROM snapshots WHERE id=?", (submission["snapshot_id"],)
        )
        return {
            "submission_id": submission["id"],
            "attempt_id": submission.get("attempt_id") or (attempt or {}).get("id"),
            "snapshot_id": submission.get("snapshot_id") or (snapshot or {}).get("id"),
            "source_sha256": snapshot_row["sha256"] if snapshot_row else submission.get("request_hash"),
            "state": submission["state"],
            "remote_id": submission.get("remote_id"),
            "reused": reused,
            "submitted": True,
            "note": "accepted/queued only; query read_official_evaluation for the result",
        }

    # ------------------------------------------------------------------
    def call(self, name: str, arguments: dict | None) -> dict:
        if name not in TOOL_NAMES:
            raise ToolArgumentError(f"unknown tool: {name}")
        args = validate_arguments(name, arguments)
        return getattr(self, name)(args)
