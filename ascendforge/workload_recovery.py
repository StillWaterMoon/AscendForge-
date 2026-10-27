"""Workload input-specification recovery for CANNJudge tasks.

The recovery runner is an independent Codex session with its own hidden task,
workspace and Codex thread. Diagnostic submissions still go through the shared
Scheduler, so CANNJudge rate limits and unknown-submission handling are identical
to optimization traffic, while optimization bests remain untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.parse
from typing import Any

from .config import Settings
from .db import Database
from .experiments import (
    add_event,
    attach_snapshot,
    create_attempt,
    get_attempt,
    read_snapshot,
    update_attempt,
)
from .scheduler import Scheduler
from .tasks import TaskError, TaskService
from .util import canonical_json, json_dumps, json_loads, new_id, now

ACTIVE_STATUSES = ("pending", "running", "paused", "external_blocked")
SPEC_STATUSES = ("verified", "partial", "unknown")
EVIDENCE_STATUSES = {
    "measured": "measured",
    "verified": "measured",
    "provided": "provided",
    "user_provided": "provided",
    "confirmed": "confirmed",
    "codex_confirmed": "confirmed",
    "inferred": "inferred",
    "unknown": "unknown",
}
TENSOR_SUFFIXES = {".npy", ".npz", ".bin", ".pt", ".pth", ".onnx", ".ckpt", ".safetensors"}
MAX_SPEC_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024


class RecoveryError(RuntimeError):
    """A user-visible workload-recovery failure."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _scope_from_url(url: str | None) -> str:
    if not url:
        return "default"
    path = urllib.parse.urlparse(url).path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) > 1:
        parts = parts[:-1]
    return "/" + "/".join(parts) if parts else "default"


def _testcase_ids(problem: dict) -> list[str] | None:
    raw = problem.get("testcases") or problem.get("testcase_ids") or problem.get("cases")
    if not isinstance(raw, list):
        return None
    ids: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            value = item.get("testcase_id") or item.get("id") or item.get("case_id") or item.get("index")
        else:
            value = item
        if value is None:
            return None
        ids.append(str(value))
    return ids or None


def _testset_key(ids: list[str] | None) -> str | None:
    if not ids:
        return None
    return hashlib.sha256(canonical_json(sorted(ids)).encode("utf-8")).hexdigest()[:24]


def _problem_version(problem: dict, task: dict) -> str | None:
    for key in ("version", "problem_version", "release", "updated_at", "revision"):
        value = _text(problem.get(key))
        if value:
            return value
    config = task.get("config") or {}
    for key in ("problem_version", "version"):
        value = _text(config.get(key))
        if value:
            return value
    return None


def fingerprint_for_task(task: dict) -> dict:
    """Stable lookup key; missing version/testset fields stay explicitly unknown."""
    problem = task.get("problem_json") or task.get("problem") or {}
    if not isinstance(problem, dict):
        problem = {}
    problem_id = _text(task.get("problem_id")) or _text(problem.get("_id")) or _text(problem.get("id"))
    if not problem_id:
        problem_id = hashlib.sha256((task.get("problem_url") or task.get("id") or "").encode("utf-8")).hexdigest()[:24]
    ids = _testcase_ids(problem)
    testset = _testset_key(ids)
    platform = _text(task.get("platform")) or "cannjudge"
    scope = _scope_from_url(task.get("problem_url"))
    version = _problem_version(problem, task)
    core = {
        "platform": platform,
        "scope": scope,
        "problem_id": problem_id,
        "problem_version": version,
        "testset_key": testset,
    }
    spec_key = hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()
    return {
        **core,
        "spec_key": spec_key,
        "testcase_ids": ids,
        "problem_url": task.get("problem_url"),
        "problem_title": task.get("problem_title") or task.get("title"),
        "unknown_fields": [key for key in ("problem_version", "testset_key") if not core.get(key)],
    }


def _normalize_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return EVIDENCE_STATUSES.get(text, "unknown")


def _shape(value: Any) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("shape 必须是数组或 null")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("shape 只能包含非负整数")
        result.append(item)
    return result


def _numel(shape: list[int] | None) -> int | None:
    if shape is None:
        return None
    value = 1
    for dim in shape:
        value *= dim
    return value


def _normalize_tensor(item: Any, *, role: str, index: int) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"{role}[{index}] 必须是对象")
    name = _text(item.get("name")) or f"{role}{index}"
    shape = _shape(item.get("shape"))
    numel_value = item.get("numel")
    numel: int | None = None
    if numel_value is not None:
        if isinstance(numel_value, bool) or not isinstance(numel_value, int) or numel_value < 0:
            raise ValueError(f"{role}.{name}.numel 必须是非负整数或 null")
        numel = numel_value
    derived_numel = _numel(shape)
    if numel is None:
        numel = derived_numel
    elif derived_numel is not None and numel != derived_numel:
        raise ValueError(f"{role}.{name}.numel 与 shape 乘积不一致")
    dtype = item.get("dtype")
    if dtype is not None and not isinstance(dtype, str):
        raise ValueError(f"{role}.{name}.dtype 必须是字符串或 null")
    return {"name": name, "dtype": dtype, "shape": shape, "numel": numel}


def _refs(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class WorkloadRecoveryService:
    def __init__(self, db: Database, settings: Settings, tasks: TaskService, scheduler: Scheduler):
        self.db = db
        self.settings = settings
        self.tasks = tasks
        self.scheduler = scheduler

    # -- lookup -------------------------------------------------------
    @staticmethod
    def _is_passing_result(normalized: dict) -> bool:
        outcome = str(normalized.get("outcome") or "").strip().lower()
        if outcome in ("passed", "pass", "all_passed"):
            return True
        raw = str(normalized.get("raw_status") or "").strip().lower()
        total = int(normalized.get("testcase_total") or 0)
        passed = int(normalized.get("testcase_passed") or 0)
        if total > 0 and passed == total:
            return True
        return raw.startswith("pass") and not normalized.get("testcase_failed")

    def passing_attempt(self, task_id: str) -> dict | None:
        task = self.tasks.get(task_id)
        candidate_ids: list[str] = []
        if task.get("best_attempt_id"):
            candidate_ids.append(task["best_attempt_id"])
        for row in self.db.query(
            "SELECT * FROM attempts WHERE task_id=? AND snapshot_id IS NOT NULL ORDER BY ordinal DESC",
            (task_id,),
        ):
            if row["id"] not in candidate_ids:
                candidate_ids.append(row["id"])
        for attempt_id in candidate_ids:
            attempt = get_attempt(self.db, attempt_id)
            if not attempt or not attempt.get("snapshot_id"):
                continue
            normalized = json_loads(attempt.get("normalized_json"), {})
            if isinstance(normalized, dict) and self._is_passing_result(normalized):
                return attempt
        return None

    def _seed_passing_base(self, hidden: dict, base: dict) -> dict:
        source_snapshot = read_snapshot(self.db, self.settings, base["snapshot_id"])
        attempt = create_attempt(
            self.db,
            hidden["id"],
            provider="recovery-base",
            prompt_version="recovery-base",
        )
        snapshot = attach_snapshot(
            self.db,
            self.settings,
            task_id=hidden["id"],
            attempt_id=attempt["id"],
            files=source_snapshot["files"],
        )
        update_attempt(
            self.db,
            attempt["id"],
            snapshot_id=snapshot["id"],
            status="finished",
            hypothesis="从优化任务的 Pass 提交恢复的只读起点",
            summary=f"base attempt {base['id']}",
            result_json=base.get("result_json"),
            normalized_json=base.get("normalized_json"),
        )
        self.db.execute(
            "UPDATE tasks SET best_attempt_id=?, performance_best_attempt_id=?, updated_at=? WHERE id=?",
            (attempt["id"], attempt["id"], now(), hidden["id"]),
        )
        add_event(
            self.db,
            hidden["id"],
            attempt["id"],
            "recovery_base_loaded",
            {"source_attempt_id": base["id"], "source_snapshot_id": base["snapshot_id"]},
        )
        return attempt

    def summary_for_task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        fp = fingerprint_for_task(task)
        spec = self.latest_spec(fp["spec_key"])
        job = self.latest_job(fp["spec_key"])
        active = self.active_job(fp["spec_key"])
        diagnostic_rows = self.diagnostics(job["id"]) if job else []
        base = self.passing_attempt(task_id)
        return {
            "fingerprint": fp,
            "spec": self.spec_public(spec) if spec else None,
            "job": self.job_public(job) if job else None,
            "active": bool(active),
            "has_cache": bool(spec and spec["status"] == "verified"),
            "diagnostics": len(diagnostic_rows),
            "diagnostic_rows": diagnostic_rows[:30],
            "can_start": bool(base),
            "base_attempt_id": base["id"] if base else None,
            "requires_pass_baseline": not bool(base),
        }

    def latest_spec(self, spec_key: str) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM workload_specs WHERE spec_key=? ORDER BY revision DESC LIMIT 1",
            (spec_key,),
        )

    def latest_job(self, spec_key: str) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM workload_recovery_jobs WHERE spec_key=? ORDER BY created_at DESC LIMIT 1",
            (spec_key,),
        )

    def active_job(self, spec_key: str) -> dict | None:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        return self.db.query_one(
            f"SELECT * FROM workload_recovery_jobs WHERE spec_key=? AND status IN ({marks}) "
            "ORDER BY created_at DESC LIMIT 1",
            (spec_key, *ACTIVE_STATUSES),
        )

    def get_job(self, job_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM workload_recovery_jobs WHERE id=?", (job_id,))
        if not row:
            raise RecoveryError("RECOVERY_NOT_FOUND", "逆向任务不存在")
        return row

    def spec_public(self, row: dict | None) -> dict | None:
        if not row:
            return None
        data = json_loads(row.get("specs_json"), {})
        cases = data.get("testcases") or []
        return {
            "id": row["id"],
            "spec_key": row["spec_key"],
            "revision": row["revision"],
            "status": row["status"],
            "platform": row["platform"],
            "scope": row["scope"],
            "problem_id": row["problem_id"],
            "problem_version": row["problem_version"],
            "testset_key": row["testset_key"],
            "testcase_ids": json_loads(row.get("testcase_ids_json"), []),
            "testcases": cases,
            "confirmed": bool(data.get("confirmed")),
            "case_count": len(cases),
            "unknown_count": sum(
                1 for case in cases if case.get("evidence_status") in ("unknown", "inferred")
            ),
            "source_job_id": row.get("source_job_id"),
            "source_task_id": row.get("source_task_id"),
            "updated_at": row.get("updated_at"),
            "validation": data.get("validation") or {},
        }

    def job_public(self, row: dict | None) -> dict | None:
        if not row:
            return None
        spec = self.latest_spec(row["spec_key"])
        task = self.db.query_one("SELECT status, config_json FROM tasks WHERE id=?", (row["workspace_task_id"],))
        config = json_loads(task.get("config_json"), {}) if task else {}
        return {
            "id": row["id"],
            "spec_key": row["spec_key"],
            "task_id": row["task_id"],
            "workspace_task_id": row["workspace_task_id"],
            "base_attempt_id": row.get("base_attempt_id"),
            "base_snapshot_id": row.get("base_snapshot_id"),
            "status": row["status"],
            "phase": config.get("codex_resume_phase") or row.get("phase"),
            "progress": json_loads(row.get("progress_json"), {}),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "workspace_path": row.get("workspace_path"),
            "log_path": row.get("log_path"),
            "codex_session_ref": row.get("codex_session_ref") or config.get("codex_session_ref"),
            "codex_runner_active": bool(config.get("codex_runner_active")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "spec_summary": self.spec_public(spec) if spec else None,
            "import": json_loads(row.get("import_json"), {}),
        }

    # -- start / reuse -------------------------------------------------
    def start(self, task_id: str, *, actor: str | None = None, force: bool = False) -> dict:
        task = self.tasks.get(task_id)
        base = self.passing_attempt(task_id)
        if not base:
            raise RecoveryError(
                "RECOVERY_REQUIRES_PASS_BASELINE",
                "逆向评测需要优化任务先产生一个 Pass 提交；当前没有可用的通过版本",
            )
        fp = fingerprint_for_task(task)
        active = self.active_job(fp["spec_key"])
        if active:
            return {"mode": "active", "fingerprint": fp, "job": self.job_public(active), "spec": self.spec_public(self.latest_spec(fp["spec_key"]))}
        spec = self.latest_spec(fp["spec_key"])
        if spec and not force:
            if spec["status"] == "verified":
                return {"mode": "cache", "fingerprint": fp, "spec": self.spec_public(spec), "job": None}
            return {"mode": "partial", "fingerprint": fp, "spec": self.spec_public(spec), "job": None}
        job_id = new_id("recovery")
        hidden_id = new_id("recoverytask")
        current = now()
        try:
            self.db.execute(
                "INSERT INTO workload_recovery_jobs(id, spec_key, task_id, workspace_task_id, base_attempt_id, "
                "base_snapshot_id, status, phase, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 'pending', 'creating_task', ?, ?)",
                (job_id, fp["spec_key"], task_id, hidden_id, base["id"], base.get("snapshot_id"), current, current),
            )
        except sqlite3.IntegrityError:
            active = self.active_job(fp["spec_key"])
            if active:
                return {"mode": "active", "fingerprint": fp, "job": self.job_public(active), "spec": self.spec_public(self.latest_spec(fp["spec_key"]))}
            raise RecoveryError("RECOVERY_LOCKED", "同一题的逆向任务正在创建，请稍后重试")
        try:
            hidden = self.tasks.create_recovery_task(task_id, job_id)
            self._seed_passing_base(hidden, base)
            workspace_task_id = hidden["id"]
            self.db.execute(
                "UPDATE workload_recovery_jobs SET workspace_task_id=?, base_attempt_id=?, base_snapshot_id=?, "
                "status='running', phase='starting_runner', updated_at=? WHERE id=?",
                (workspace_task_id, base["id"], base.get("snapshot_id"), now(), job_id),
            )
        except Exception as exc:
            self.db.execute(
                "UPDATE workload_recovery_jobs SET status='failed', phase='create_failed', error_code='RECOVERY_CREATE_FAILED', error_message=?, updated_at=?, finished_at=? WHERE id=?",
                (str(exc)[:2000], now(), now(), job_id),
            )
            raise RecoveryError("RECOVERY_CREATE_FAILED", f"创建逆向工作区失败：{exc}") from exc
        add_event(self.db, task_id, None, "workload_recovery_started", {"job_id": job_id, "spec_key": fp["spec_key"]})
        try:
            self.launch(job_id)
        except Exception as exc:
            self._set_job(job_id, status="failed", phase="launch_failed", error_code="RECOVERY_LAUNCH_FAILED", error_message=str(exc)[:2000], finished_at=now())
            raise RecoveryError("RECOVERY_LAUNCH_FAILED", f"启动逆向 Codex 失败：{exc}") from exc
        return {"mode": "started", "fingerprint": fp, "job": self.job_public(self.get_job(job_id)), "spec": None}

    # -- runner launch / lifecycle -------------------------------------
    def _set_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        names = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE workload_recovery_jobs SET {names} WHERE id=?", (*fields.values(), job_id))

    def launch(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job["status"] in ("paused", "external_blocked", "completed", "partial", "failed"):
            return job
        hidden = self.tasks.get(job["workspace_task_id"])
        if (hidden.get("config") or {}).get("codex_runner_active"):
            return job
        log_path = self.settings.logs_dir / f"recovery-{job_id}-{int(now())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-m", "ascendforge", "recovery-run", "--job", job_id]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.settings.root)
        env["ASCENDFORGE_DATA_DIR"] = str(self.settings.data_dir)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        handle = open(log_path, "a", encoding="utf-8")
        try:
            process = subprocess.Popen(command, cwd=str(self.settings.root), stdout=handle, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        except Exception:
            self._set_job(job_id, status="failed", phase="launch_failed", error_code="RECOVERY_LAUNCH_FAILED", error_message="cannot start recovery runner", finished_at=now())
            raise
        finally:
            handle.close()
        self._set_job(job_id, status="running", phase="starting_runner", log_path=str(log_path), codex_runner_pid=process.pid, started_at=job.get("started_at") or now())
        return self.get_job(job_id)

    def pause(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job["status"] not in ACTIVE_STATUSES:
            raise RecoveryError("BAD_STATE", f"逆向任务当前为 {job['status']}，不能暂停")
        try:
            self.tasks.pause(job["workspace_task_id"], reason="workload_recovery_pause")
        except TaskError:
            pass
        self._set_job(job_id, status="paused", phase="pause_requested", codex_pause_requested=1)
        return self.get_job(job_id)

    def resume(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job["status"] not in ("paused", "external_blocked", "partial"):
            raise RecoveryError("BAD_STATE", f"逆向任务当前为 {job['status']}，不能继续")
        try:
            source_pass = self.passing_attempt(job["task_id"])
        except TaskError:
            source_pass = None
        if not job.get("base_attempt_id") or not source_pass:
            raise RecoveryError(
                "RECOVERY_REQUIRES_PASS_BASELINE",
                "该逆向任务没有从 Pass 提交创建的基座；请先让优化任务产生 Pass，再重新探测",
            )
        hidden_id = job["workspace_task_id"]
        self.tasks.reconcile_codex_runner(hidden_id)
        hidden = self.tasks.get(hidden_id)
        if hidden["status"] == "paused":
            self.tasks.resume(hidden_id)
        elif hidden["status"] != "running":
            raise RecoveryError("BAD_STATE", f"逆向工作区任务状态为 {hidden['status']}，不能继续")
        self._set_job(job_id, status="running", phase="resuming", codex_pause_requested=0, finished_at=None)
        hidden = self.tasks.get(hidden_id)
        if not (hidden.get("config") or {}).get("codex_runner_active"):
            self.launch(job_id)
        return self.get_job(job_id)

    def stop(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if job["status"] in ("completed", "partial", "failed"):
            return job
        try:
            self.tasks.stop(job["workspace_task_id"], reason="workload_recovery_stop")
        except TaskError:
            pass
        self._set_job(
            job_id,
            status="failed",
            phase="stopped",
            error_code="RECOVERY_STOPPED",
            error_message="用户停止逆向任务",
            finished_at=now(),
        )
        return self.get_job(job_id)

    # -- structured import --------------------------------------------
    def run_job(self, job_id: str) -> dict:
        from .codex_loop import run_codex_agent  # local import avoids a cycle

        job = self.get_job(job_id)
        hidden_id = job["workspace_task_id"]
        hidden = self.tasks.get(hidden_id)
        if hidden["status"] == "paused":
            return {"job_id": job_id, "status": "paused"}
        startup_extra = (
            f"逆向基座：优化任务 Pass attempt {job.get('base_attempt_id') or 'unknown'} 的完整源码快照已复制到当前 workspace，"
            "不要从空模板重新开始。\n"
            "首次启动顺序：先调用 read_problem(url) 阅读完整题面，再读取当前 workspace 的模板/基座代码，"
            "然后读取 .ascendforge/skills/oj-feedback-reverse-engineering/SKILL.md（按需读 references），"
            "按照技能内的方法设计探针；之后才允许诊断提交。"
            "为节省 token，只写一个最小 specifications.json，不需要 evidence.md、长篇说明或证据文件。"
            "顶层必须写 confirmed:true 表示你已确认规格；宿主会记录来源任务和诊断提交。格式："
            '{"schema":"ascendforge-workload-spec/1","confirmed":true,"testcases":[{"testcase_id":"...",'
            '"inputs":[{"name":"x","dtype":"float16","shape":[1,1024],"numel":1024}],'
            '"outputs":[],"attributes":{},"derived":{}}]}。'
            "未知字段写 null，不要编造；不要生成 tensor 文件。"
        )
        error: str | None = None
        error_code: str | None = None
        try:
            run_codex_agent(
                self.settings,
                hidden_id,
                mode="recovery",
                skill_name="oj-feedback-reverse-engineering",
                startup_extra=startup_extra,
                skip_preflight=True,
                embed_skill=False,
                timeout_sec=3600,
            )
        except Exception as exc:
            error = str(exc)
            error_code = getattr(exc, "code", None) or "RECOVERY_RUNNER_FAILED"
        try:
            imported = self.import_job(job_id)
        except RecoveryError as exc:
            imported = {"ok": False, "error": exc.message, "warnings": []}
            error = exc.message
            error_code = exc.code
            self._set_job(job_id, import_json=json_dumps(imported))
        current_task = self.tasks.get(hidden_id)
        spec = self.db.query_one(
            "SELECT * FROM workload_specs WHERE source_job_id=? ORDER BY revision DESC LIMIT 1",
            (job_id,),
        )
        blockers = {"ACCOUNT_CANNOT_CONNECT_JUDGE", "CODEX_THREAD_LOCKED", "SUBMISSION_UNKNOWN"}
        if spec and spec["status"] == "verified":
            status = "completed"
        elif error and (error_code in blockers or current_task["status"] in ("needs_login", "needs_review")):
            status = "external_blocked"
        elif current_task["status"] == "paused":
            status = "paused"
        elif error:
            status = "partial" if spec and spec["status"] == "partial" else "failed"
        elif not spec:
            status = "failed"
            error_code = error_code or "RECOVERY_NO_SPEC"
            error = error or "Codex 未生成可导入的 specifications.json"
        else:
            status = "partial"
        self._set_job(
            job_id,
            status=status,
            phase="imported" if spec else "no_spec",
            finished_at=now(),
            error_code=error_code,
            error_message=(error or "")[:2000] or None,
            codex_session_ref=(current_task.get("config") or {}).get("codex_session_ref"),
            specs_json=spec["specs_json"] if spec else None,
        )
        return {"job_id": job_id, "status": status, "imported": imported, "error": error}

    def import_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        task = self.tasks.get(job["workspace_task_id"])
        workspace = self.settings.data_dir / "cli_workspaces" / job["workspace_task_id"] / "workspace"
        spec_path = workspace / "specifications.json"
        evidence_path = workspace / "evidence.md"
        if not spec_path.is_file():
            result = {"ok": False, "error": "specifications.json not found", "warnings": []}
            self._set_job(job_id, import_json=json_dumps(result), progress_json=json_dumps({"import": result}))
            return result
        if spec_path.stat().st_size > MAX_SPEC_BYTES:
            raise RecoveryError("SPEC_TOO_LARGE", "specifications.json 超过 2 MiB")
        tensor_files = [
            path for path in workspace.rglob("*")
            if path.is_file() and path.suffix.lower() in TENSOR_SUFFIXES and path.stat().st_size > 1024 * 1024
        ]
        if tensor_files:
            raise RecoveryError(
                "TENSOR_ARTIFACT_FORBIDDEN",
                "逆向工作区包含超过 1 MiB 的 tensor 文件；本阶段只允许保存小型 JSON/Markdown/源码/日志",
                {"files": [str(path.relative_to(workspace)) for path in tensor_files[:10]]},
            )
        try:
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            result = {"ok": False, "error": f"specifications.json 无法解析：{exc}", "warnings": []}
            self._set_job(job_id, import_json=json_dumps(result))
            return result
        evidence = evidence_path.read_text(encoding="utf-8")[:MAX_EVIDENCE_BYTES] if evidence_path.is_file() else ""
        try:
            normalized = self._normalize_spec(raw, task, job)
        except RecoveryError:
            raise
        except (TypeError, ValueError) as exc:
            raise RecoveryError("BAD_SPEC", str(exc)[:1000]) from exc
        fp = fingerprint_for_task(task)
        revision = int(self.db.scalar("SELECT COALESCE(MAX(revision), 0) FROM workload_specs WHERE spec_key=?", (fp["spec_key"],)) or 0) + 1
        spec_id = new_id("workload")
        current = now()
        self.db.execute(
            "INSERT INTO workload_specs(id, spec_key, revision, platform, scope, problem_id, problem_version, "
            "testset_key, testcase_ids_json, status, specs_json, evidence_md, source_job_id, source_task_id, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                spec_id, fp["spec_key"], revision, fp["platform"], fp["scope"], fp["problem_id"],
                fp["problem_version"], fp["testset_key"], canonical_json(fp.get("testcase_ids") or []),
                normalized["status"], json_dumps(normalized), evidence, job_id, job["task_id"], current, current,
            ),
        )
        result = {"ok": True, "spec_id": spec_id, "revision": revision, "status": normalized["status"], "warnings": normalized.get("warnings", [])}
        self._set_job(job_id, specs_json=json_dumps(normalized), evidence_md=evidence, import_json=json_dumps(result), progress_json=json_dumps({"import": result}))
        add_event(self.db, job["task_id"], None, "workload_recovery_imported", {"job_id": job_id, "spec_id": spec_id, "status": normalized["status"]})
        return result

    def _normalize_spec(self, raw: Any, task: dict, job: dict) -> dict:
        if not isinstance(raw, dict):
            raise RecoveryError("BAD_SPEC", "specifications.json 顶层必须是对象")
        cases_raw = raw.get("testcases")
        if isinstance(cases_raw, dict):
            cases_raw = [{"testcase_id": key, **(value if isinstance(value, dict) else {})} for key, value in cases_raw.items()]
        if not isinstance(cases_raw, list) or not cases_raw:
            raise RecoveryError("BAD_SPEC", "specifications.json 必须包含非空 testcases 数组")
        confirmed = bool(raw.get("confirmed") or raw.get("codex_confirmed"))
        problem = task.get("problem_json") or {}
        expected = _testcase_ids(problem) or []
        expected_set = {str(item) for item in expected}
        evidence_tasks = (job["workspace_task_id"], job["task_id"])
        marks = ",".join("?" for _ in evidence_tasks)
        submissions = {row["id"] for row in self.db.query(f"SELECT id FROM submissions WHERE task_id IN ({marks})", evidence_tasks)}
        attempts = {row["id"] for row in self.db.query(f"SELECT id FROM attempts WHERE task_id IN ({marks})", evidence_tasks)}
        snapshots = {row["id"] for row in self.db.query(f"SELECT id FROM snapshots WHERE task_id IN ({marks})", evidence_tasks)}
        valid_refs = submissions | attempts | snapshots
        cases: list[dict] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(cases_raw):
            if not isinstance(item, dict):
                raise RecoveryError("BAD_SPEC", f"testcases[{index}] 必须是对象")
            case_id = _text(item.get("testcase_id") or item.get("id") or item.get("case_id"))
            if not case_id:
                raise RecoveryError("BAD_SPEC", f"testcases[{index}] 缺少 testcase_id")
            if case_id in seen:
                warnings.append(f"重复 testcase_id：{case_id}")
            seen.add(case_id)
            inputs_raw = item.get("inputs") or []
            outputs_raw = item.get("outputs") or []
            if isinstance(inputs_raw, dict):
                inputs_raw = [{"name": key, **(value if isinstance(value, dict) else {})} for key, value in inputs_raw.items()]
            if isinstance(outputs_raw, dict):
                outputs_raw = [{"name": key, **(value if isinstance(value, dict) else {})} for key, value in outputs_raw.items()]
            if not isinstance(inputs_raw, list) or not isinstance(outputs_raw, list):
                raise RecoveryError("BAD_SPEC", f"{case_id}: inputs/outputs 必须是数组")
            inputs = [_normalize_tensor(value, role="input", index=i) for i, value in enumerate(inputs_raw)]
            outputs = [_normalize_tensor(value, role="output", index=i) for i, value in enumerate(outputs_raw)]
            evidence_status = _normalize_status(item.get("evidence_status"))
            if confirmed and evidence_status in ("unknown", "provided"):
                evidence_status = "confirmed"
            refs_raw = _refs(item.get("evidence_refs"))
            refs: list[Any] = []
            refs_ok = True
            for ref in refs_raw:
                if isinstance(ref, dict):
                    ref_id = next((str(ref[key]) for key in ("submission_id", "attempt_id", "snapshot_id") if ref.get(key)), None)
                else:
                    ref_id = str(ref)
                if not ref_id or ref_id not in valid_refs:
                    refs_ok = False
                    warnings.append(f"{case_id}: 证据引用不存在：{ref_id or '(empty)'}")
                    refs.append(ref)
                else:
                    refs.append(ref)
            if evidence_status == "measured" and not refs_ok:
                evidence_status = "confirmed" if confirmed else "inferred"
                warnings.append(f"{case_id}: measured 缺少有效证据，降级为 {evidence_status}")
            if evidence_status == "measured" and not refs:
                evidence_status = "confirmed" if confirmed else "inferred"
                warnings.append(f"{case_id}: measured 没有证据引用，降级为 {evidence_status}")
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            derived = item.get("derived") if isinstance(item.get("derived"), dict) else {}
            cases.append({
                "testcase_id": case_id,
                "inputs": inputs,
                "outputs": outputs,
                "attributes": attributes,
                "derived": derived,
                "evidence_status": evidence_status,
                "evidence_refs": refs,
            })
        measured = [case for case in cases if case["evidence_status"] == "measured"]
        confirmed_cases = [case for case in cases if case["evidence_status"] == "confirmed"]
        covered = bool(expected_set) and expected_set.issubset(seen)
        if expected_set and not covered:
            warnings.append("未覆盖题目声明的全部测试点")
        if not expected_set:
            warnings.append("题目未提供 testcase ID 集合，无法声明完整覆盖")
        all_have_inputs = all(case["inputs"] for case in cases)
        all_specified = all(
            tensor.get("dtype") is not None
            and tensor.get("shape") is not None
            and tensor.get("numel") is not None
            for case in cases
            for tensor in case["inputs"] + case["outputs"]
        )
        claimed = str(raw.get("status") or "").strip().lower()
        all_confirmed = bool(confirmed) and len(confirmed_cases) + len(measured) == len(cases)
        measured_verified = bool(measured) and len(measured) == len(cases)
        if (measured_verified or all_confirmed) and covered and all_have_inputs and all_specified:
            status = "verified"
        elif measured or confirmed_cases or any(case["inputs"] for case in cases):
            status = "partial"
        else:
            status = "unknown"
        if claimed in SPEC_STATUSES and claimed != status:
            warnings.append(f"声明状态 {claimed} 与证据计算状态 {status} 不一致，采用 {status}")
        unknown_fields = sum(
            1 for case in cases
            for tensor in case["inputs"] + case["outputs"]
            for key in ("dtype", "shape", "numel")
            if tensor.get(key) is None
        )
        return {
            "schema": "ascendforge-workload-spec/1",
            "status": status,
            "confirmed": confirmed,
            "testcases": cases,
            "validation": {
                "expected_testcase_ids": sorted(expected_set),
                "covered_testcase_ids": sorted(seen),
                "measured_cases": len(measured),
                "confirmed_cases": len(confirmed_cases),
                "unknown_fields": unknown_fields,
                "warnings": warnings,
            },
            "warnings": warnings,
            "imported_at": now(),
        }

    def diagnostics(self, job_id: str) -> list[dict]:
        job = self.get_job(job_id)
        rows = self.db.query(
            "SELECT a.id AS attempt_id, a.ordinal, a.hypothesis, a.diagnostic, s.id AS submission_id, "
            "s.state, s.remote_id, s.error_code FROM attempts a LEFT JOIN submissions s ON s.attempt_id=a.id "
            "WHERE a.task_id=? ORDER BY a.ordinal DESC",
            (job["workspace_task_id"],),
        )
        return [dict(row) for row in rows]

    def cleanup_for_task(self, task_id: str) -> list[str]:
        """Remove recovery jobs/hidden tasks owned by a deleted optimization task.

        Spec cache rows are intentionally shared and remain available for other
        tasks working on the same problem/version.
        """
        warnings: list[str] = []
        jobs = self.db.query(
            "SELECT * FROM workload_recovery_jobs WHERE task_id=? ORDER BY created_at", (task_id,)
        )
        for job in jobs:
            try:
                if job["status"] in ACTIVE_STATUSES:
                    self.stop(job["id"])
            except Exception as exc:
                warnings.append(f"stop {job['id']}: {exc}")
            hidden_id = job.get("workspace_task_id")
            if hidden_id:
                try:
                    hidden = self.tasks.get(hidden_id)
                    if hidden["status"] == "running":
                        self.tasks.stop(hidden_id, reason="source_task_deleted")
                    self.tasks.delete(hidden_id, actor="recovery_cleanup")
                except TaskError as exc:
                    warnings.append(f"delete {hidden_id}: {exc.message}")
                except Exception as exc:
                    warnings.append(f"delete {hidden_id}: {exc}")
            self.db.execute("DELETE FROM workload_recovery_jobs WHERE id=?", (job["id"],))
        return warnings

    def diagnostics_for_task(self, task_id: str) -> list[dict]:
        fp = fingerprint_for_task(self.tasks.get(task_id))
        job = self.latest_job(fp["spec_key"])
        return self.diagnostics(job["id"]) if job else []
