"""Local Codex optimization loop.

This is the first usable CLI-agent loop:

host workspace -> codex exec/edit -> host snapshot -> scheduler submission
-> real result -> session resume -> next revision.

It intentionally does not let Codex submit to CANNJudge or touch the platform
database.  The worker is still the only sender of official submissions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .agent.change_audit import audit_candidate
from .agent.preflight import preflight_candidate
from .cli_agent.app_server import AppServerAdapter
from .cli_agent.codex import CodexRunResult
from .config import Settings
from .db import Database
from .experiment_service import ensure_active
from .experiments import (
    add_event,
    attach_snapshot,
    create_attempt,
    current_attempt,
    get_attempt,
    read_snapshot,
    update_attempt,
)
from .platform.cannjudge import CannjudgeAdapter
from .platform.service import PlatformService
from .scheduler import Scheduler
from .security import CredentialStore, SecretBox, load_master_key
from .tasks import TaskService
from .util import json_dumps, json_loads, new_id, now
from .worker import Worker


class CodexLoopError(RuntimeError):
    pass


# Codex 只暴露三个平台工具：
# 读题、提交官方、查询官方结果。
# 源码读取/编辑使用 Codex 自己的 workspace 文件能力，不再提供额外文件工具。
CLI_TOOL_SPECS = [
    {
        "type": "function",
        "name": "read_problem",
        "description": "Read a CANNJudge problem by its public URL and return the complete Markdown statement.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "CANNJudge problem URL.",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_official_evaluation",
        "description": "Read a CANNJudge submission by submission_id: state, compile errors, testcase results, timing and score.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "submission_id": {"type": "string", "minLength": 1, "maxLength": 120}
            },
            "required": ["submission_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "submit_official",
        "description": "Snapshot the current workspace and submit it to CANNJudge through the host scheduler. Returns submission_id immediately; query it with read_official_evaluation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Optional idempotency key for safe retries.",
                },
                "hypothesis": {"type": "string", "maxLength": 4000},
                "parent_attempt_id": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
]


def _services(settings: Settings) -> tuple[Database, TaskService, Scheduler, Worker]:
    db = Database(settings.db_path)
    db.init()
    box = SecretBox(load_master_key(settings.key_file, ""))
    credentials = CredentialStore(db, box)
    platform = PlatformService(db, settings, credentials)
    tasks = TaskService(db, settings, platform)
    scheduler = Scheduler(db, settings, platform, tasks)
    worker = Worker(settings)
    return db, tasks, scheduler, worker


def _workspace_files(workspace: Path) -> dict[str, str]:
    files = {}
    for path in sorted(Path(workspace).rglob("*")):
        if not path.is_file():
            continue
        if path.name in ("TASK.json", "AGENTS.md", "codex-tools.json"):
            continue
        files[str(path.relative_to(workspace)).replace("\\", "/")] = path.read_text(encoding="utf-8")
    return files


def _template_files(task: dict) -> dict[str, str]:
    template = json_loads(task.get("template_json"), {})
    return {item["path"]: item["content"] for item in CannjudgeAdapter._template_files(template)}


def _parent_source(db: Database, settings: Settings, task: dict) -> tuple[str | None, dict[str, str]]:
    experiment = ensure_active(db, task["id"])
    parent = (
        experiment.get("working_attempt_id")
        or experiment.get("baseline_attempt_id")
        or task.get("performance_best_attempt_id")
        or task.get("best_attempt_id")
    )
    attempt = get_attempt(db, parent) if parent else None
    if attempt and attempt.get("snapshot_id"):
        return attempt["id"], read_snapshot(db, settings, attempt["snapshot_id"])["files"]
    return None, _template_files(task)


def _record_run(db: Database, *, task_id: str, attempt_id: str | None, workspace: Path | None, result: CodexRunResult, changed: list[str], started: float) -> str:
    run_id = new_id("clirun")
    db.insert(
        "INSERT INTO cli_runs(id, task_id, attempt_id, backend, session_ref, run_id, workspace, "
        "exit_code, timed_out, usage_json, last_message, changed_files_json, error, started_at, finished_at) "
        "VALUES(?, ?, ?, 'codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            task_id,
            attempt_id,
            result.session_ref,
            result.run_id,
            str(workspace) if workspace else None,
            result.exit_code,
            1 if result.timed_out else 0,
            json_dumps(result.usage),
            result.last_message[-8000:],
            json_dumps(changed),
            result.error,
            started,
            now(),
        ),
    )
    return run_id


def _wait_submission(db: Database, scheduler: Scheduler, worker: Worker, submission_id: str, timeout_sec: float) -> dict:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        worker.process_queued_submissions()
        scheduler.poll_due()
        row = db.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,))
        if row and row["state"] in ("finished", "failed", "unknown"):
            return row
        time.sleep(2)
    raise CodexLoopError(f"submission {submission_id} did not finish within {timeout_sec}s")


def _codex_thread_name(task: dict) -> str:
    """生成 Codex 会话名：题目名 + 算子优化。"""
    problem = task.get("problem_json") if isinstance(task.get("problem_json"), dict) else {}
    name = (
        task.get("problem_title")
        or problem.get("name")
        or problem.get("title")
        or task.get("title")
        or "算子"
    )
    name = str(name).strip()
    if name.endswith("算子优化"):
        return name[:120]
    return f"{name}算子优化"[:120]


def _submission_summary(
    db: Database,
    submission: dict,
    *,
    reused: bool,
    attempt: dict | None = None,
    snapshot: dict | None = None,
) -> dict:
    snapshot_row = db.query_one(
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
        "note": "accepted/queued only; poll read_official_evaluation for the result",
    }


def run_codex_agent(
    settings: Settings,
    task_id: str,
    *,
    prompt: str = "",
    timeout_sec: float = 1800,
    submission_timeout: float = 1200,
    keep_running: bool = False,
    lock_token: str | None = None,
) -> dict:
    """Tool-driven Codex session; no fixed round count.

    Codex owns the conversation and decides when to call save/submit/query
    tools. The host owns workspace preparation, immutable snapshots, scheduler
    rate limits and the prompt/session lifetime.
    """
    db, tasks, scheduler, worker = _services(settings)
    task = tasks.get(task_id)
    if task["status"] == "pending":
        tasks.start(task_id)
    elif task["status"] == "paused":
        tasks.resume(task_id)
    elif task["status"] != "running":
        raise CodexLoopError(f"task is {task['status']}; use Web continue/start before Codex loop")
    config = dict(task.get("config") or {})
    token = lock_token or new_id("codexrun")
    if config.get("codex_runner_active") and config.get("codex_runner_token") not in (None, token):
        raise CodexLoopError("another Codex runner owns this task")
    config.update(
        {
            "execution_mode": "codex",
            "codex_runner_active": True,
            "codex_runner_token": token,
            "codex_runner_status": "running",
        }
    )
    db.execute(
        "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
        (json_dumps(config), now(), task_id),
    )
    experiment = ensure_active(db, task_id)
    parent_id, parent_files = _parent_source(db, settings, task)
    workspace_root = Path(settings.data_dir) / "cli_workspaces" / task_id
    workspace = workspace_root / "workspace"
    if not workspace.exists():
        # The adapter is needed for prepare_workspace; defer actual creation
        # until after the app-server is available.
        pass
    session_ref: str | None = config.get("codex_session_ref")
    state = {
        "parent_id": parent_id,
        "parent_files": dict(parent_files),
        "saved_attempt_id": None,
        "last_submission_id": None,
        "last_result": None,
    }
    history: list[dict] = []
    adapter: AppServerAdapter | None = None

    def on_event(event) -> None:
        if event.type == "thread.started" and event.raw.get("thread_id"):
            config["codex_session_ref"] = str(event.raw["thread_id"])
            config["codex_runner_status"] = "running"
            db.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (json_dumps(config), now(), task_id),
            )
            add_event(db, task_id, None, "codex_thread_started", {"thread_id": config["codex_session_ref"]})

    def collect_effective() -> tuple[dict[str, str], dict]:
        workspace_files = {}
        for path in sorted(workspace.rglob("*")):
            if path.is_file() and path.name not in ("TASK.json", "AGENTS.md", "codex-tools.json"):
                workspace_files[str(path.relative_to(workspace)).replace("\\", "/")] = path.read_text(
                    encoding="utf-8"
                )
        changes = {
            path: content
            for path, content in workspace_files.items()
            if content != state["parent_files"].get(path)
        }
        if not changes:
            raise ValueError("workspace has no source changes to save")
        files = dict(state["parent_files"])
        files.update(workspace_files)
        audit = audit_candidate(state["parent_files"], files)
        if not audit["effective"]:
            raise ValueError("workspace changes are comments/whitespace only")
        check = preflight_candidate(files, json_loads(task.get("problem_json"), {}))
        if not check["ok"]:
            raise ValueError("preflight failed: " + "; ".join(item["message"] for item in check["errors"][:5]))
        return files, audit

    def tool_handler(tool: str, arguments: dict, raw: dict) -> dict | str:
        if tool == "read_problem":
            # Codex 按 URL 读取公开题面；返回 Markdown 原文，不包 JSON。
            url = str(arguments.get("url") or "").strip()
            if not url:
                raise ValueError("read_problem requires url")
            adapter = CannjudgeAdapter(
                base_url=settings.cannjudge_base_url,
                timeout=30,
            )
            problem = adapter.resolve_problem(url)
            statement = str(problem.get("desc") or problem.get("description") or "")
            if not statement.strip():
                raise ValueError("problem page has no statement")
            return statement
        if tool == "read_official_evaluation":
            submission_id = str(arguments.get("submission_id") or "").strip()
            if not submission_id:
                raise ValueError("read_official_evaluation requires submission_id")
            row = db.query_one(
                "SELECT * FROM submissions WHERE id=? AND task_id=?",
                (submission_id, task_id),
            )
            if not row:
                raise ValueError("submission not found for this task")
            attempt = get_attempt(db, row.get("attempt_id") or "") if row.get("attempt_id") else None
            snapshot = (
                db.query_one("SELECT sha256 FROM snapshots WHERE id=?", (row["snapshot_id"],))
                if row.get("snapshot_id")
                else None
            )
            normalized = json_loads(attempt.get("normalized_json"), {}) if attempt else {}
            return {
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
        if tool == "submit_official":
            # 只保存快照并排队，立即返回 submission_id；不阻塞等待远端结果。
            # Codex 之后调用 read_official_evaluation 查询。
            if tasks.get(task_id)["status"] != "running":
                raise ValueError("task must be running to submit_official")
            request_id = str(arguments.get("request_id") or "").strip() or None
            if request_id:
                existing = db.query_one(
                    "SELECT * FROM submissions WHERE task_id=? AND eval_key=? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (task_id, request_id),
                )
                if existing:
                    return _submission_summary(db, existing, reused=True)
            files, audit = collect_effective()
            hypothesis = str(arguments.get("hypothesis") or "Codex official candidate").strip()[:4000]
            parent_id = str(arguments.get("parent_attempt_id") or state["parent_id"] or "") or None
            attempt = create_attempt(
                db,
                task_id,
                parent_attempt_id=parent_id,
                experiment_id=experiment["id"],
                provider="codex-cli",
                model="codex",
                prompt_version="codex-tool-v1",
            )
            update_attempt(
                db,
                attempt["id"],
                hypothesis=hypothesis,
                status="local_checking",
                changed_files_json=json_dumps(
                    {"files": sorted(files), "source": "codex-workspace", "audit": audit}
                ),
            )
            snapshot = attach_snapshot(
                db,
                settings,
                task_id=task_id,
                attempt_id=attempt["id"],
                files=files,
            )
            update_attempt(db, attempt["id"], snapshot_id=snapshot["id"], status="generated")
            submission = scheduler.request_submission(task_id, attempt["id"])
            if request_id:
                db.execute(
                    "UPDATE submissions SET eval_key=?, updated_at=? WHERE id=?",
                    (request_id, now(), submission["id"]),
                )
                submission = db.query_one("SELECT * FROM submissions WHERE id=?", (submission["id"],))
            state["parent_id"] = attempt["id"]
            state["parent_files"] = dict(files)
            state["saved_attempt_id"] = attempt["id"]
            state["last_submission_id"] = submission["id"]
            return _submission_summary(
                db,
                submission,
                reused=False,
                attempt=attempt,
                snapshot=snapshot,
            )
        if tool == "task_status":
            current = tasks.get(task_id)
            return {
                "task_id": task_id,
                "status": current["status"],
                "best_attempt_id": current.get("best_attempt_id"),
                "performance_best_attempt_id": current.get("performance_best_attempt_id"),
                "experiment": experiment.get("id"),
                "parent_attempt_id": state["parent_id"],
                "last_submission_id": state["last_submission_id"],
            }
        if tool == "read_history":
            limit = max(1, min(30, int(arguments.get("limit") or 10)))
            rows = db.query(
                "SELECT ordinal,id,status,hypothesis,normalized_json,error_code FROM attempts "
                "WHERE task_id=? ORDER BY ordinal DESC LIMIT ?",
                (task_id, limit),
            )
            return [
                {
                    "ordinal": row["ordinal"],
                    "attempt_id": row["id"],
                    "status": row["status"],
                    "hypothesis": (row.get("hypothesis") or "")[:1000],
                    "normalized": json_loads(row.get("normalized_json"), {}),
                    "error_code": row.get("error_code"),
                }
                for row in rows
            ]
        if tool == "read_attempt_source":
            attempt_id = str(arguments.get("attempt_id") or state["parent_id"] or "")
            attempt = get_attempt(db, attempt_id) if attempt_id else None
            if not attempt or not attempt.get("snapshot_id"):
                raise ValueError("attempt has no source snapshot")
            return read_snapshot(db, settings, attempt["snapshot_id"])["files"]
        if tool == "read_workspace":
            return _workspace_files(workspace)
        if tool in ("save_candidate", "submit_candidate"):
            files, audit = collect_effective()
            hypothesis = str(arguments.get("hypothesis") or "Codex candidate").strip()[:4000]
            attempt = create_attempt(
                db,
                task_id,
                parent_attempt_id=state["parent_id"],
                experiment_id=experiment["id"],
                provider="codex-cli",
                model="codex",
                prompt_version="codex-tool-v1",
            )
            update_attempt(
                db,
                attempt["id"],
                hypothesis=hypothesis,
                status="local_checking",
                changed_files_json=json_dumps(
                    {"files": sorted(files), "source": "codex-workspace", "audit": audit}
                ),
            )
            snapshot = attach_snapshot(
                db,
                settings,
                task_id=task_id,
                attempt_id=attempt["id"],
                files=files,
            )
            update_attempt(db, attempt["id"], snapshot_id=snapshot["id"], status="generated")
            state["parent_id"] = attempt["id"]
            state["parent_files"] = dict(files)
            state["saved_attempt_id"] = attempt["id"]
            if tool == "save_candidate":
                return {
                    "attempt_id": attempt["id"],
                    "snapshot_id": snapshot["id"],
                    "submitted": False,
                    "hypothesis": hypothesis,
                }
            submission = scheduler.request_submission(task_id, attempt["id"])
            row = _wait_submission(db, scheduler, worker, submission["id"], submission_timeout)
            normalized = json_loads(
                db.query_one("SELECT normalized_json FROM attempts WHERE id=?", (attempt["id"],))[
                    "normalized_json"
                ],
                {},
            )
            state["last_submission_id"] = submission["id"]
            state["last_result"] = normalized
            return {
                "attempt_id": attempt["id"],
                "snapshot_id": snapshot["id"],
                "submission_id": submission["id"],
                "state": row["state"],
                "remote_id": row.get("remote_id"),
                "result": normalized,
            }
        if tool == "query_submission":
            submission_id = arguments.get("submission_id") or state["last_submission_id"]
            if not submission_id:
                return {"state": None, "note": "no submission has been made"}
            row = db.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,))
            if not row:
                raise ValueError("submission not found")
            attempt = get_attempt(db, row.get("attempt_id") or "")
            return {
                "submission_id": row["id"],
                "state": row["state"],
                "remote_id": row.get("remote_id"),
                "error_code": row.get("error_code"),
                "normalized": json_loads(attempt.get("normalized_json"), {}) if attempt else {},
            }
        raise ValueError(f"unknown tool: {tool}")

    turn_error: str | None = None
    try:
        adapter = AppServerAdapter(
            timeout_sec=timeout_sec,
            on_event=on_event,
            on_tool_call=tool_handler,
            tools=CLI_TOOL_SPECS,
            thread_name=_codex_thread_name(task),
        )
        if not workspace.exists():
            workspace = adapter.prepare_workspace(
                workspace_root,
                parent_files,
                task_meta={
                    "task_id": task_id,
                    "title": task.get("title"),
                    "problem_title": task.get("problem_title"),
                    "problem_url": task.get("problem_url"),
                    "performance_best": task.get("performance_best_attempt_id"),
                },
                instructions="Use the provided dynamic tools; do not attempt to submit outside them.",
            )
        # 首次启动注入真实 SKILL.md 和 TOOLS.md，而不是占位文本。
        skill_path = Path(settings.root) / "skills" / "start-operator-optimization" / "SKILL.md"
        tools_path = Path(settings.root) / "skills" / "start-operator-optimization" / "TOOLS.md"
        skill_text = skill_path.read_text(encoding="utf-8") if skill_path.is_file() else "SKILL.md 未找到"
        tools_text = tools_path.read_text(encoding="utf-8") if tools_path.is_file() else "TOOLS.md 未找到"
        template = task.get("template") or {}
        template_files = [
            item for item in (template.get("files") or []) if isinstance(item, dict)
        ]
        editable_files = [str(item.get("path")) for item in template_files if item.get("editable")]
        readonly_files = [str(item.get("path")) for item in template_files if not item.get("editable")]
        project_source = str(config.get("project_source") or "empty")
        if project_source == "upload":
            template_info = (
                f"项目模板：已上传并解压到 {settings.data_dir / 'tasks' / task_id / 'project-template'}\n"
            )
        else:
            template_info = "项目模板：未上传，workspace 从空项目开始，需要自行创建源码和构建文件。\n"
        startup = (
            "下面是首次启动必须完整阅读的 skill 和工具合同。\n\n"
            "===== SKILL.md =====\n"
            + skill_text
            + "\n\n===== TOOLS.md =====\n"
            + tools_text
            + "\n\n===== 任务启动信息 =====\n"
            + f"任务 ID：{task_id}\n工作目录：{workspace}\n"
            + f"题目链接：{task.get('problem_url') or ''}\n"
            + template_info
            + f"允许提交文件：{', '.join(editable_files) if editable_files else '由 Codex 按题面创建；根目录 .asc/.h'}\n"
            + f"只读文件：{', '.join(readonly_files) if readonly_files else '无额外只读文件'}\n"
            + f"评分指标：{config.get('metric') or 'auto'}；目标：{config.get('target_score')}\n"
            + "可用工具只有三个：\n"
            + "1. read_problem(url)：读取完整题面；\n"
            + "2. submit_official(request_id?, hypothesis?)：保存当前 workspace 快照并排队官方提交，立即返回 submission_id；\n"
            + "3. read_official_evaluation(submission_id)：查询提交状态、编译错误、测试点、耗时和分数。\n"
            + "源码请直接用你自己的文件工具在 workspace 内读取和修改；不要等待用户逐步指挥。\n"
            + "只修改可编辑文件：kernel.asc 和根目录 .asc/.h；不要修改 main.asc、CMakeLists.txt、run.sh、sample.py、reference.py。\n"
            + "不要设置累计轮数、token/费用/时长预算；由用户手动暂停或停止。\n"
            + "提交后必须轮询 read_official_evaluation，根据真实结果决定修复、继续、回退或更换方向。\n\n"
            + (prompt or "")
        )
        turns = 0
        while True:
            current = tasks.get(task_id)
            if current["status"] != "running":
                break
            instruction = startup if turns == 0 and not session_ref else (
                "继续自主优化。读取最新提交结果，决定下一步；使用工具保存/提交候选。"
                "不要等待用户逐步指挥，直到用户暂停或停止。"
            )
            if session_ref:
                result = adapter.resume(
                    session_ref, instruction, workspace=workspace, output_file=workspace_root / "last.txt"
                )
            else:
                result = adapter.start(
                    instruction,
                    workspace=workspace,
                    output_file=workspace_root / "last.txt",
                    sandbox="workspace-write",
                )
            session_ref = result.session_ref or session_ref
            if session_ref:
                config["codex_session_ref"] = session_ref
                db.execute(
                    "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                    (json_dumps(config), now(), task_id),
                )
            _record_run(
                db,
                task_id=task_id,
                attempt_id=state.get("saved_attempt_id"),
                workspace=workspace,
                result=result,
                changed=sorted(_workspace_files(workspace)),
                started=now(),
            )
            turns += 1
            # exit_code=0 不代表成功：Codex 的 403 余额不足等 API 错误
            # 会以 result.error 返回，但进程退出码仍可能是 0。
            if result.error or result.timed_out or result.exit_code not in (0, None):
                turn_error = str(result.error or ("timeout" if result.timed_out else "codex exited non-zero"))
                add_event(
                    db,
                    task_id,
                    None,
                    "codex_turn_failed",
                    {"error": turn_error, "stderr": result.stderr[-1000:]},
                )
                break
            history.append({"turn": turns, "thread": session_ref, "last": result.last_message[:1000]})
            if tasks.get(task_id)["status"] != "running":
                break
            time.sleep(2)
        return {"task_id": task_id, "turns": turns, "history": history, "session_ref": session_ref}
    finally:
        if adapter:
            try:
                adapter.close()
            except Exception:
                pass
        try:
            current_task = tasks.get(task_id)
            current_config = dict(current_task.get("config") or {})
            owns_token = current_config.get("codex_runner_token") == token
            if owns_token:
                current_config.pop("codex_runner_active", None)
                current_config.pop("codex_runner_pid", None)
                current_config.pop("codex_runner_token", None)
                current_config["codex_runner_status"] = "finished"
            current_config["execution_mode"] = "codex"
            db.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (json_dumps(current_config), now(), task_id),
            )
            if turn_error and owns_token and current_task["status"] == "running":
                # 余额不足/网络断开等真实错误不能让任务一直显示 running
                # 却没有 runner。暂停后用户可以充值并手动恢复同一会话。
                tasks.pause(task_id, reason=f"codex_error: {turn_error[:300]}")
                db.execute(
                    "UPDATE tasks SET last_error_code='CODEX_ERROR', last_error=?, updated_at=? WHERE id=?",
                    (turn_error[:2000], now(), task_id),
                )
                add_event(
                    db,
                    task_id,
                    None,
                    "codex_session_paused_on_error",
                    {"error": turn_error[:1000]},
                )
            # 正常结束/用户暂停不自动暂停；真实错误才落为 paused。
        except Exception:
            pass


def run_codex_loop(
    settings: Settings,
    task_id: str,
    *,
    rounds: int = 3,
    prompt: str = "",
    timeout_sec: float = 900,
    submission_timeout: float = 1200,
    keep_running: bool = False,
    lock_token: str | None = None,
) -> dict:
    db, tasks, scheduler, worker = _services(settings)
    task = tasks.get(task_id)
    if task["status"] == "pending":
        tasks.start(task_id)
    elif task["status"] == "paused":
        tasks.resume(task_id)
    elif task["status"] != "running":
        raise CodexLoopError(f"task is {task['status']}; use Web continue/start before Codex loop")
    config = dict(task.get("config") or {})
    token = lock_token or new_id("codexrun")
    if config.get("codex_runner_active") and config.get("codex_runner_token") not in (None, token):
        raise CodexLoopError("another Codex runner owns this task")
    config["execution_mode"] = "codex"
    config["codex_runner_active"] = True
    config["codex_runner_token"] = token
    config["codex_runner_status"] = "starting"
    db.execute(
        "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
        (json_dumps(config), now(), task_id),
    )
    def _on_event(event) -> None:
        if event.type == "thread.started" and event.raw.get("thread_id"):
            config["codex_session_ref"] = str(event.raw["thread_id"])
            config["codex_runner_status"] = "running"
            db.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (json_dumps(config), now(), task_id),
            )
            add_event(
                db,
                task_id,
                None,
                "codex_thread_started",
                {"thread_id": config["codex_session_ref"]},
            )

    adapter = AppServerAdapter(timeout_sec=timeout_sec, on_event=_on_event)
    experiment = ensure_active(db, task_id)
    base_parent, base_files = _parent_source(db, settings, task)
    workspace_root = Path(settings.data_dir) / "cli_workspaces" / task_id
    session_ref: str | None = config.get("codex_session_ref")
    history: list[dict] = []
    try:
        for round_index in range(1, max(1, rounds) + 1):
            current = get_attempt(db, base_parent) if base_parent else None
            parent_id = current["id"] if current and current.get("snapshot_id") else None
            attempt = create_attempt(
                db,
                task_id,
                parent_attempt_id=parent_id,
                experiment_id=experiment["id"],
                provider="codex-cli",
                model="codex",
                prompt_version="codex-workspace-v1",
            )
            parent_files = dict(base_files)
            task_meta = {
                "task_id": task_id,
                "title": task.get("title"),
                "problem_title": task.get("problem_title"),
                "problem_url": task.get("problem_url"),
                "attempt_id": attempt["id"],
                "attempt_ordinal": attempt["ordinal"],
                "baseline_attempt": experiment.get("baseline_attempt_id"),
                "performance_best": task.get("performance_best_attempt_id"),
                "history": history[-4:],
            }
            workspace = adapter.prepare_workspace(
                workspace_root,
                parent_files,
                task_meta=task_meta,
                instructions=(
                    "You are editing an Ascend C operator candidate. "
                    "Only modify source files under workspace/. Keep the task interface and all dtypes correct. "
                    "Do not access credentials, the platform database, or the official judge. "
                    "The host will save and submit the changed snapshot; do not submit yourself.\n"
                ),
            )
            instruction = prompt or (
                "Inspect TASK.json, AGENTS.md and the current source. Make one focused, reviewable optimization "
                "that should improve performance while preserving correctness. Keep every dtype supported by the "
                "task. Modify only files under workspace/. At the end, summarize the actual code changes."
            )
            if history:
                instruction += (
                    "\nPrevious result: "
                    + json.dumps(history[-1], ensure_ascii=False)[:3000]
                    + "\nContinue from the current workspace; repair it if the result was compile/WA."
                )
            if session_ref:
                result = adapter.resume(
                    session_ref,
                    instruction,
                    workspace=workspace,
                    output_file=workspace_root / "last.txt",
                )
                if result.exit_code != 0:
                    session_ref = None
                    result = adapter.start(
                        instruction,
                        workspace=workspace,
                        output_file=workspace_root / "last.txt",
                        sandbox="workspace-write",
                    )
            else:
                result = adapter.start(
                    instruction,
                    workspace=workspace,
                    output_file=workspace_root / "last.txt",
                    sandbox="workspace-write",
                )
            session_ref = result.session_ref or session_ref
            if session_ref:
                config["codex_session_ref"] = session_ref
                db.execute(
                    "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                    (json_dumps(config), now(), task_id),
                )
            changes = adapter.collect_changes(workspace, parent_files)
            run_started = now()
            _record_run(
                db,
                task_id=task_id,
                attempt_id=attempt["id"],
                workspace=workspace,
                result=result,
                changed=sorted(changes),
                started=run_started,
            )
            if result.timed_out or result.exit_code != 0:
                update_attempt(
                    db,
                    attempt["id"],
                    status="failed",
                    error_code="CODEX_RUN_FAILED",
                    error_message=(result.error or result.stderr or "codex exited non-zero")[:2000],
                    finished_at=now(),
                )
                raise CodexLoopError(f"Codex run failed: {result.error or result.stderr[-500:]}")
            if not changes:
                update_attempt(
                    db,
                    attempt["id"],
                    status="failed",
                    error_code="CODEX_NO_CHANGES",
                    error_message="Codex did not modify any workspace source file",
                    finished_at=now(),
                )
                raise CodexLoopError("Codex made no source changes")

            files = dict(parent_files)
            files.update(changes)
            observed = audit_candidate(parent_files, files)
            if not observed["effective"]:
                update_attempt(
                    db,
                    attempt["id"],
                    status="failed",
                    error_code="CODEX_NO_EFFECTIVE_CHANGE",
                    error_message="Codex changed only comments/whitespace; no source behavior changed",
                    finished_at=now(),
                )
                raise CodexLoopError("Codex changed only comments/whitespace")
            check = preflight_candidate(files, json_loads(task.get("problem_json"), {}))
            if not check["ok"]:
                update_attempt(
                    db,
                    attempt["id"],
                    status="failed",
                    error_code="CODEX_PREFLIGHT_FAILED",
                    error_message="; ".join(item["message"] for item in check["errors"][:5])[:2000],
                    finished_at=now(),
                )
                history.append(
                    {
                        "attempt": attempt["ordinal"],
                        "kind": "preflight_failed",
                        "errors": check["errors"][:3],
                    }
                )
                base_parent = experiment.get("working_attempt_id") or experiment.get("baseline_attempt_id")
                base_files = parent_files
                continue
            hypothesis = result.last_message.strip()[:4000] or "Codex workspace edit"
            update_attempt(
                db,
                attempt["id"],
                hypothesis=hypothesis,
                status="local_checking",
                changed_files_json=json_dumps(
                    {"files": sorted(changes), "source": "codex-workspace", "preflight": check}
                ),
            )
            snapshot = attach_snapshot(
                db,
                settings,
                task_id=task_id,
                attempt_id=attempt["id"],
                files=files,
            )
            update_attempt(db, attempt["id"], snapshot_id=snapshot["id"], status="generated")
            submission = scheduler.request_submission(task_id, attempt["id"])
            row = _wait_submission(db, scheduler, worker, submission["id"], submission_timeout)
            normalized = json_loads(
                db.query_one("SELECT normalized_json FROM attempts WHERE id=?", (attempt["id"],))["normalized_json"],
                {},
            )
            history.append(
                {
                    "attempt": attempt["ordinal"],
                    "submission": row["state"],
                    "outcome": normalized.get("outcome"),
                    "passed": normalized.get("testcase_passed"),
                    "total": normalized.get("testcase_total"),
                    "message": (normalized.get("message") or "")[:1000],
                    "time_values": normalized.get("time_values") or [],
                }
            )
            base_parent = attempt["id"]
            base_files = read_snapshot(db, settings, snapshot["id"])["files"]
            if normalized.get("outcome") not in ("passed", "failed"):
                # A compile/runtime error is a repair input, not a direction verdict.
                continue
        return {"task_id": task_id, "rounds": rounds, "history": history, "session_ref": session_ref}
    finally:
        try:
            adapter.close()
        except Exception:
            pass
        try:
            current_task = tasks.get(task_id)
            current_config = dict(current_task.get("config") or {})
            if current_config.get("codex_runner_token") == token:
                current_config.pop("codex_runner_active", None)
                current_config.pop("codex_runner_pid", None)
                current_config.pop("codex_runner_token", None)
                current_config["codex_runner_status"] = "finished"
            current_config["execution_mode"] = "codex"
            db.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (json_dumps(current_config), now(), task_id),
            )
            if not keep_running and current_task["status"] == "running":
                tasks.pause(task_id, reason="codex_loop_finished")
        except Exception:
            pass
