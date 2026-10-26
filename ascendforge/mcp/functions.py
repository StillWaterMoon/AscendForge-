"""MCP server 背后的五个 tool function 实现。

工具只返回数据或提交 ID，不负责猜测结果：
- 服务器测试和官方 OJ 是两条完全不同的链路；
- 官方提交只调用宿主 Scheduler，不直接访问 CANNJudge；
- 读文件只允许题目模板和当前草稿目录，防止路径穿越。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..db import Database
from ..experiments import (
    attach_snapshot,
    create_attempt,
    get_attempt,
    read_snapshot,
    update_attempt,
)
from ..platform.cannjudge import CannjudgeAdapter
from ..platform.service import PlatformService
from ..scheduler import Scheduler
from ..security import CredentialStore, SecretBox, load_master_key
from ..tasks import TaskService
from ..util import json_loads, now, safe_relpath
from .contracts import TOOL_NAMES, ToolArgumentError, validate_arguments
from .server_eval import ServerEvaluationError, ServerEvaluationService

# 只收集源码/构建相关文本文件；build 目录和元数据文件不进入快照。
SOURCE_SUFFIXES = {".asc", ".h", ".hpp", ".cpp", ".cc", ".py", ".sh", ".txt", ".json", ".cmake"}
META_NAMES = {"TASK.json", "AGENTS.md", "codex-tools.json", "codex-prompt.txt"}
MAX_SOURCE_FILES = 64
MAX_SOURCE_BYTES = 2 * 1024 * 1024


class MCPToolError(RuntimeError):
    pass


class MCPToolFunctions:
    """绑定单个任务的 tool function 集合。

    MCP server 启动时确定 task_id 和 workspace，因此模型不能在调用参数里
    换任务或读取其他目录。所有提交都使用该任务现有快照/调度器。
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
        problem_reader: Callable[[str], dict] | None = None,
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
        # read_problem 按 URL 读取公开题面；默认用无凭据的 CANNJudge 适配器。
        self.problem_reader = problem_reader or self._read_public_problem
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
        # 收集草稿中的文本源码；跳过 Codex 的元数据和隐藏/构建产物。
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
        # 优先当前草稿；草稿不存在时退回最近候选快照，最后退回题目模板。
        # 这样即使 Codex 还没写文件，读题和首次提交也不会凭空失败。
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

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------
    def _read_public_problem(self, url: str) -> dict:
        # CANNJudge 前端是 SPA，真正的题面在公开 API 里。用无 cookie 的
        # adapter 按页面 URL 解析到 problem 对象，不读取用户凭据。
        adapter = CannjudgeAdapter(
            base_url=self.settings.cannjudge_base_url,
            timeout=30,
        )
        return adapter.resolve_problem(url)

    def read_problem(self, arguments: dict) -> str:
        # 输入只接受题目 URL；返回值也只要完整题面。
        # CANNJudge 页面是 SPA，正文从公开 API 的 desc 字段读取。
        url = str(arguments["url"]).strip()
        try:
            problem = self.problem_reader(url)
        except Exception as exc:
            raise MCPToolError(f"题目页面解析失败: {type(exc).__name__}: {exc}") from exc
        if not isinstance(problem, dict) or not problem:
            raise MCPToolError("题目页面没有返回有效题目数据")
        statement = str(problem.get("desc") or problem.get("description") or "")
        if not statement.strip():
            raise MCPToolError("题目页面没有可解析的题面内容")
        return statement

    # ------------------------------------------------------------------
    # Evaluation reads
    # ------------------------------------------------------------------
    def read_server_evaluation(self, arguments: dict) -> dict:
        # 服务器测试结果单独存放，绝不能伪装成官方 normalized 结果。
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
        # 保存快照后才提交；返回的 source_sha256 用于确认结果对应哪份源码。
        # 服务器评测是异步的，所以这里只确认“已接收”，不等待结果。
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
        # 官方提交严格走 Scheduler：限频、额度、在途和 unknown 对账都在
        # 调度器里执行，MCP 不能绕过，也不能直接碰 CANNJudge。
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
    def call(self, name: str, arguments: dict | None) -> Any:
        if name not in TOOL_NAMES:
            raise ToolArgumentError(f"unknown tool: {name}")
        args = validate_arguments(name, arguments)
        return getattr(self, name)(args)
