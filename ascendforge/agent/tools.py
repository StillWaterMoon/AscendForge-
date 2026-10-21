"""Host-side implementation of every Agent tool.

The model proposes; this module validates ownership/state/arguments and performs
the effect.  It never gives the model a shell, a network client or a platform
credential.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from ..execution.capabilities import detect_capabilities
from ..execution.executor import ExecutorError, get_executor
from ..experiments import (
    SnapshotError,
    add_event,
    attach_snapshot,
    attempt_history,
    create_snapshot,
    get_attempt,
    normalize_result,
    read_snapshot,
    record_model_call,
    snapshot_files,
    update_attempt,
    update_local_best,
)
from ..platform.base import PlatformError
from ..platform.cannjudge import CannjudgeAdapter, FORBIDDEN_KERNEL_FILES, FORBIDDEN_PATH_PARTS
from ..platform.service import PlatformService
from ..scheduler import Scheduler
from ..states import ATTEMPT_FAILED, ATTEMPT_FINISHED
from ..tasks import TaskError, TaskService
from ..util import canonical_json, json_dumps, json_loads, new_id, now, sha256_bytes, truncate_text
from .protocol import Action, ActionError, tool_result_error, tool_result_ok

log = logging.getLogger(__name__)

MAX_TOOL_FILES = 128
MAX_TOOL_FILE_BYTES = 2 * 1024 * 1024


class ToolFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


VALIDATION_HARNESS = r'''#!/usr/bin/env python3
"""Host-provided harness: run generator.py then reference.py in one sandbox."""
import json, os, subprocess, sys, time, hashlib

def run(args, timeout):
    started = time.time()
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        out = p.stdout[-20000:].decode("utf-8", "replace")
        err = p.stderr[-20000:].decode("utf-8", "replace")
        return p.returncode, out, err, int((time.time()-started)*1000)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")[-20000:].decode("utf-8", "replace")
        err = (e.stderr or b"")[-20000:].decode("utf-8", "replace")
        return None, out, err, int((time.time()-started)*1000)

def main():
    seed = sys.argv[1]
    timeout = int(sys.argv[2])
    os.makedirs("out", exist_ok=True)
    gen_rc, gen_out, gen_err, gen_ms = run(
        [sys.executable, "generator.py", "--seed", seed, "--out", "out"], timeout
    )
    manifest = os.path.join("out", "manifest.json")
    gen_ok = gen_rc == 0 and os.path.isfile(manifest)
    ref_rc = ref_out = ref_err = None
    ref_ms = None
    ref_ok = False
    if gen_ok:
        ref_rc, ref_out, ref_err, ref_ms = run(
            [sys.executable, "reference.py", "--seed", seed, "--manifest", manifest, "--out", "out/reference"],
            timeout,
        )
        ref_json = os.path.join("out", "reference", "reference.json")
        ref_ok = ref_rc == 0 and os.path.isfile(ref_json)
    files = []
    for root, dirs, names in os.walk("out"):
        for name in names:
            path = os.path.join(root, name)
            try:
                data = open(path, "rb").read()
                files.append({"path": path.replace(os.sep, "/"), "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()})
            except OSError:
                pass
    result = {
        "generator_ok": gen_ok,
        "reference_ok": ref_ok,
        "generator_exit": gen_rc,
        "reference_exit": ref_rc,
        "generator_ms": gen_ms,
        "reference_ms": ref_ms,
        "generator_stdout": gen_out[-8000:],
        "generator_stderr": gen_err[-8000:],
        "reference_stdout": (ref_out or "")[-8000:],
        "reference_stderr": (ref_err or "")[-8000:],
        "files": files[:200],
    }
    with open("out/validation.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k not in ("files",)}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


class ToolExecutor:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        platform: PlatformService,
        tasks: TaskService,
        scheduler: Scheduler,
    ):
        self.db = db
        self.settings = settings
        self.platform = platform
        self.tasks = tasks
        self.scheduler = scheduler

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def execute(self, action: Action, *, task_id: str, attempt_id: str) -> dict:
        handlers = {
            "read_problem": self._read_problem,
            "read_history": self._read_history,
            "create_candidate": self._create_candidate,
            "create_test_generator": self._create_test_generator,
            "run_local_validation": self._run_local_validation,
            "request_submission": self._request_submission,
            "query_result": self._query_result,
            "finish_task": self._finish_task,
        }
        handler = handlers.get(action.tool_name)
        if not handler:
            return tool_result_error("UNKNOWN_TOOL", f"unknown tool: {action.tool_name}")
        attempt = self._attempt(task_id, attempt_id)
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return tool_result_error("TASK_NOT_FOUND", "task does not exist", retryable=False)
        try:
            data = handler(task, attempt, action.arguments)
            return tool_result_ok(data)
        except ToolFailure as exc:
            return tool_result_error(exc.code, exc.message, retryable=exc.retryable)
        except (TaskError, PlatformError, ActionError, SnapshotError) as exc:
            return tool_result_error(
                getattr(exc, "code", "TOOL_ERROR"),
                getattr(exc, "message", str(exc)),
                retryable=bool(getattr(exc, "retryable", False)),
            )
        except Exception as exc:
            log.exception("tool execution failed: %s", action.tool_name)
            return tool_result_error("TOOL_FAILED", f"{type(exc).__name__}: {str(exc)[:400]}", retryable=True)

    def _attempt(self, task_id: str, attempt_id: str) -> dict:
        attempt = get_attempt(self.db, attempt_id)
        if not attempt or attempt["task_id"] != task_id:
            raise ToolFailure("BAD_ATTEMPT", "attempt does not belong to the task", retryable=False)
        return attempt

    @staticmethod
    def _require_running(task: dict) -> None:
        if task["status"] != "running":
            raise ToolFailure("TASK_NOT_RUNNING", f"task is {task['status']}; no new model/tool work", retryable=False)

    def _decode(self, task: dict) -> tuple[dict, dict]:
        return (
            json_loads(task.get("problem_json"), {}),
            json_loads(task.get("template_json"), {}),
        )

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------
    def _read_problem(self, task: dict, attempt: dict, arguments: dict) -> dict:
        problem, template = self._decode(task)
        include = bool(arguments.get("include_contents", True))
        template_files = CannjudgeAdapter._template_files(template)
        files = []
        for item in template_files:
            entry = {"path": item["path"], "editable": item["editable"], "bytes": len(item["content"].encode())}
            if include and item["editable"]:
                entry["content"] = item["content"]
            files.append(entry)
        return {
            "problem_id": problem.get("_id") or task.get("problem_id"),
            "problem_title": problem.get("name") or task.get("problem_title"),
            "code_template": problem.get("code_template"),
            "problem": {
                key: problem.get(key)
                for key in (
                    "name",
                    "description",
                    "content",
                    "input",
                    "output",
                    "constraints",
                    "time_limit",
                    "memory_limit",
                    "difficulty",
                    "tags",
                    "score",
                )
                if problem.get(key) is not None
            },
            "template_files": files,
            "readonly_files": [f["path"] for f in template_files if not f["editable"]],
            "forbidden_files": sorted(FORBIDDEN_KERNEL_FILES),
            "notes": [
                "Editable template contents are authoritative; do not substitute another problem's signature.",
                "Read-only template files must not be modified or submitted.",
            ],
        }

    def _read_history(self, task: dict, attempt: dict, arguments: dict) -> dict:
        limit = max(1, min(50, int(arguments.get("limit") or 20)))
        attempts = attempt_history(self.db, task["id"], limit=limit)
        history = []
        for item in attempts:
            normalized = json_loads(item.get("normalized_json"), {})
            snapshot = None
            if item.get("snapshot_id"):
                row = self.db.query_one("SELECT sha256, file_count, total_bytes FROM snapshots WHERE id=?", (item["snapshot_id"],))
                snapshot = row
            submission = self.db.query_one(
                "SELECT id, state, remote_id, error_code, error_message FROM submissions "
                "WHERE attempt_id=? ORDER BY created_at DESC LIMIT 1",
                (item["id"],),
            )
            history.append(
                {
                    "id": item["id"],
                    "ordinal": item["ordinal"],
                    "parent_attempt_id": item["parent_attempt_id"],
                    "status": item["status"],
                    "hypothesis": item["hypothesis"],
                    "snapshot": snapshot,
                    "submission": submission,
                    "outcome": normalized.get("outcome"),
                    "raw_status": normalized.get("raw_status"),
                    "testcase_passed": normalized.get("testcase_passed"),
                    "testcase_total": normalized.get("testcase_total"),
                    "score": normalized.get("score"),
                    "precision_ratio": normalized.get("precision_ratio"),
                    "message": normalized.get("message") or item.get("error_message"),
                    "error_code": item.get("error_code"),
                    "local_validation": json_loads(item.get("local_validation_json"), None),
                }
            )
        task_row = self.db.query_one("SELECT best_attempt_id, best_metric_json FROM tasks WHERE id=?", (task["id"],))
        return {
            "best_attempt_id": task_row["best_attempt_id"] if task_row else None,
            "best_metric": json_loads(task_row.get("best_metric_json"), None) if task_row else None,
            "attempts": history,
            "remote_best_note": "Only a measured passing remote result can be best; local/CPU reference results are separate.",
        }

    # ------------------------------------------------------------------
    # Candidate / generator
    # ------------------------------------------------------------------
    def _create_candidate(self, task: dict, attempt: dict, arguments: dict) -> dict:
        self._require_running(task)
        if attempt.get("snapshot_id"):
            raise ToolFailure("SNAPSHOT_EXISTS", "this attempt already has a candidate snapshot; request its submission or start a new attempt")
        files: dict[str, str] = {}
        raw_files = arguments.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ToolFailure("BAD_FILES", "files must be a non-empty array", retryable=False)
        if len(raw_files) > MAX_TOOL_FILES:
            raise ToolFailure("TOO_MANY_FILES", f"at most {MAX_TOOL_FILES} files", retryable=False)
        total = 0
        for item in raw_files:
            if not isinstance(item, dict):
                raise ToolFailure("BAD_FILES", "each file must be an object", retryable=False)
            path = str(item.get("path") or "").replace("\\", "/").strip()
            content = item.get("content")
            if not path or not isinstance(content, str):
                raise ToolFailure("BAD_FILES", "each file needs path and string content", retryable=False)
            if path in files:
                raise ToolFailure("DUPLICATE_FILE", f"duplicate path: {path}", retryable=False)
            size = len(content.encode("utf-8"))
            if size > MAX_TOOL_FILE_BYTES:
                raise ToolFailure("FILE_TOO_LARGE", f"{path} exceeds {MAX_TOOL_FILE_BYTES} bytes", retryable=False)
            total += size
            files[path] = content
        if total > 8 * 1024 * 1024:
            raise ToolFailure("CANDIDATE_TOO_LARGE", "candidate exceeds 8 MiB", retryable=False)

        problem, template = self._decode(task)
        code_template = str(problem.get("code_template") or "registry")
        filled: list[str] = []
        if code_template == "npu_kernel_dev":
            template_files = CannjudgeAdapter._template_files(template)
            editable = {item["path"]: item["content"] for item in template_files if item["editable"]}
            for path, content in editable.items():
                if path not in files:
                    files[path] = content
                    filled.append(path)
            payload = CannjudgeAdapter._kernel_files(template, files)
            files = {item["path"]: item["content"] for item in payload}
        else:
            CannjudgeAdapter._registry_fields(files)

        hypothesis = str(arguments.get("hypothesis") or "").strip()
        if not hypothesis:
            raise ToolFailure("BAD_HYPOTHESIS", "hypothesis is required", retryable=False)
        parent = arguments.get("parent_attempt_id")
        if parent:
            parent_attempt = get_attempt(self.db, str(parent))
            if not parent_attempt or parent_attempt["task_id"] != task["id"]:
                raise ToolFailure("BAD_PARENT", "parent_attempt_id does not belong to this task", retryable=False)
        snapshot = attach_snapshot(
            self.db,
            self.settings,
            task_id=task["id"],
            attempt_id=attempt["id"],
            files=files,
        )
        fields = {"hypothesis": hypothesis[:4000]}
        if parent:
            fields["parent_attempt_id"] = parent
        update_attempt(self.db, attempt["id"], **fields)
        add_event(
            self.db,
            task["id"],
            attempt["id"],
            "candidate_created",
            {
                "snapshot_id": snapshot["id"],
                "sha256": snapshot["sha256"],
                "files": snapshot["file_count"],
                "bytes": snapshot["total_bytes"],
                "hypothesis": hypothesis[:500],
            },
        )
        return {
            "snapshot_id": snapshot["id"],
            "sha256": snapshot["sha256"],
            "file_count": snapshot["file_count"],
            "total_bytes": snapshot["total_bytes"],
            "files": json_loads(snapshot["files_json"], []),
            "filled_from_template": filled,
            "next": "Call request_submission to enqueue this exact snapshot. Do not modify it in place.",
        }

    def _create_test_generator(self, task: dict, attempt: dict, arguments: dict) -> dict:
        self._require_running(task)
        generator_source = arguments.get("generator_source")
        reference_source = arguments.get("reference_source")
        if not isinstance(generator_source, str) or not generator_source.strip():
            raise ToolFailure("BAD_GENERATOR", "generator_source is required", retryable=False)
        if not isinstance(reference_source, str) or not reference_source.strip():
            raise ToolFailure("BAD_REFERENCE", "reference_source is required", retryable=False)
        if len(generator_source.encode()) > 200_000 or len(reference_source.encode()) > 200_000:
            raise ToolFailure("SOURCE_TOO_LARGE", "generator/reference source exceeds 200 KiB", retryable=False)
        seed = arguments.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ToolFailure("BAD_SEED", "seed must be a non-negative integer", retryable=False)
        spec = arguments.get("spec") if isinstance(arguments.get("spec"), dict) else {}
        validation = arguments.get("validation") if isinstance(arguments.get("validation"), dict) else {}
        generator_snapshot = create_snapshot(
            self.db,
            self.settings,
            task_id=task["id"],
            attempt_id=attempt["id"],
            files={"generator.py": generator_source},
            kind="generator",
        )
        reference_snapshot = create_snapshot(
            self.db,
            self.settings,
            task_id=task["id"],
            attempt_id=attempt["id"],
            files={"reference.py": reference_source},
            kind="reference",
        )
        generator_id = new_id("gen")
        self.db.execute(
            "INSERT INTO generators(id, task_id, attempt_id, generator_snapshot_id, reference_snapshot_id, "
            "seed, spec_json, validation_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generator_id,
                task["id"],
                attempt["id"],
                generator_snapshot["id"],
                reference_snapshot["id"],
                seed,
                canonical_json(spec),
                canonical_json(validation),
                now(),
            ),
        )
        update_attempt(self.db, attempt["id"], generator_id=generator_id)
        add_event(
            self.db,
            task["id"],
            attempt["id"],
            "generator_created",
            {
                "generator_id": generator_id,
                "seed": seed,
                "generator_sha256": generator_snapshot["sha256"],
                "reference_sha256": reference_snapshot["sha256"],
            },
        )
        return {
            "generator_id": generator_id,
            "seed": seed,
            "generator_sha256": generator_snapshot["sha256"],
            "reference_sha256": reference_snapshot["sha256"],
            "next": "Call run_local_validation with this generator_id.",
        }

    # ------------------------------------------------------------------
    # Local validation
    # ------------------------------------------------------------------
    def _run_local_validation(self, task: dict, attempt: dict, arguments: dict) -> dict:
        self._require_running(task)
        generator_id = str(arguments.get("generator_id") or "")
        row = self.db.query_one("SELECT * FROM generators WHERE id=? AND task_id=?", (generator_id, task["id"]))
        if not row:
            raise ToolFailure("GENERATOR_NOT_FOUND", "generator does not exist for this task", retryable=False)
        try:
            generator_files = snapshot_files(self.db, self.settings, row["generator_snapshot_id"])
            reference_files = snapshot_files(self.db, self.settings, row["reference_snapshot_id"])
        except Exception as exc:
            raise ToolFailure("GENERATOR_SNAPSHOT", f"generator/reference snapshot unreadable: {exc}", retryable=False) from None
        config = json_loads(task.get("config_json"), {})
        timeout = int(config.get("max_local_seconds") or 30)
        capabilities = detect_capabilities(self.settings)
        files = {
            **generator_files,
            **reference_files,
            "harness.py": VALIDATION_HARNESS,
        }
        try:
            executor = get_executor(self.settings)
        except ExecutorError as exc:
            validation = {
                "status": "executor_unavailable",
                "generator_ok": False,
                "reference_ok": False,
                "candidate_executed": False,
                "candidate_status": "unavailable",
                "error": str(exc),
                "seed": row["seed"],
                "spec": json_loads(row["spec_json"], {}),
            }
            self._record_validation(task, attempt, row, validation)
            return validation
        python = "python" if executor.name == "docker" else sys.executable
        result = executor.run(
            files,
            [python, "harness.py", str(row["seed"]), str(max(5, timeout // 2))],
            timeout=timeout + 10,
            memory_mb=self.settings.executor_memory_mb,
            cpus=self.settings.executor_cpus,
            allow_network=False,
        )
        harness_payload = {}
        try:
            if "out/validation.json" in result.artifacts:
                harness_payload = json_loads(result.artifacts["out/validation.json"].decode("utf-8"), {})
        except Exception:
            harness_payload = {}
        generator_ok = bool(harness_payload.get("generator_ok"))
        reference_ok = bool(harness_payload.get("reference_ok"))
        if result.timed_out:
            status = "timeout"
        elif not generator_ok:
            status = "generator_failed"
        elif not reference_ok:
            status = "reference_failed"
        else:
            status = "reference_ok"
        validation = {
            "status": status,
            "generator_ok": generator_ok,
            "reference_ok": reference_ok,
            "candidate_executed": False,
            "candidate_status": "unavailable",
            "candidate_note": "No NPU/CANN execution path is available; CPU reference success is not an Ascend C pass.",
            "seed": row["seed"],
            "spec": json_loads(row["spec_json"], {}),
            "rules": json_loads(row["validation_json"], {}),
            "backend": result.backend,
            "network_enforced": result.network_enforced,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "executor": result.public(),
            "harness": {k: v for k, v in harness_payload.items() if k != "files"},
            "artifacts": harness_payload.get("files") or [],
            "capabilities": {
                "npu": capabilities.get("npu"),
                "candidate_execution": capabilities.get("candidate_execution"),
            },
            "limitations": [
                "Generated code runs in the server executor, never on the user's computer.",
                "Without an NPU, the candidate status is untested; only data/reference execution is validated.",
                "Reference correctness is checked against the problem's sample only when the agent encodes such checks; a self-consistent reference is not a proof.",
            ],
        }
        self._record_validation(task, attempt, row, validation)
        return validation

    def _record_validation(self, task: dict, attempt: dict, row: dict, validation: dict) -> None:
        self.db.execute(
            "UPDATE generators SET last_validation_json=? WHERE id=?",
            (canonical_json(validation), row["id"]),
        )
        update_attempt(self.db, attempt["id"], local_validation_json=canonical_json(validation))
        update_local_best(self.db, task["id"], attempt["id"], validation)
        add_event(
            self.db,
            task["id"],
            attempt["id"],
            "local_validation",
            {
                "generator_id": row["id"],
                "status": validation.get("status"),
                "candidate_executed": validation.get("candidate_executed"),
            },
        )

    # ------------------------------------------------------------------
    # Submission (request only)
    # ------------------------------------------------------------------
    def _request_submission(self, task: dict, attempt: dict, arguments: dict) -> dict:
        self._require_running(task)
        if not attempt.get("snapshot_id"):
            raise ToolFailure("NO_SNAPSHOT", "create_candidate first; only an immutable snapshot can be submitted")
        submission = self.scheduler.request_submission(task["id"], attempt["id"])
        return {
            "submission_id": submission["id"],
            "state": submission["state"],
            "snapshot_id": submission["snapshot_id"],
            "note": "Queued. The host scheduler enforces quotas and sends the exact snapshot bytes; do not request again for this attempt.",
        }

    def _query_result(self, task: dict, attempt: dict, arguments: dict) -> dict:
        submission_id = arguments.get("submission_id")
        if submission_id:
            submission = self.db.query_one(
                "SELECT * FROM submissions WHERE id=? AND task_id=?", (str(submission_id), task["id"])
            )
        else:
            submission = self.db.query_one(
                "SELECT * FROM submissions WHERE attempt_id=? ORDER BY created_at DESC LIMIT 1",
                (attempt["id"],),
            )
        if not submission:
            return {"submission": None, "note": "No submission has been requested for this attempt."}
        attempt_row = get_attempt(self.db, submission["attempt_id"])
        normalized = json_loads(attempt_row.get("normalized_json"), None) if attempt_row else None
        raw = json_loads(submission.get("response_json"), None)
        return {
            "submission_id": submission["id"],
            "state": submission["state"],
            "remote_id": submission.get("remote_id"),
            "error_code": submission.get("error_code"),
            "error_message": submission.get("error_message"),
            "normalized": normalized,
            "raw": raw,
            "note": "Host-side state only. The scheduler polls CANNJudge; do not assume a result before state=finished.",
        }

    def _finish_task(self, task: dict, attempt: dict, arguments: dict) -> dict:
        reason = str(arguments.get("reason") or "other")
        summary = str(arguments.get("summary") or "").strip()
        if not summary:
            raise ToolFailure("BAD_SUMMARY", "summary is required", retryable=False)
        update_attempt(
            self.db,
            attempt["id"],
            status=ATTEMPT_FINISHED,
            summary=summary[:4000],
            finished_at=now(),
        )
        add_event(
            self.db,
            task["id"],
            attempt["id"],
            "agent_finished",
            {"reason": reason, "summary": summary[:2000]},
        )
        self.tasks.complete(task["id"], f"agent_finished:{reason}")
        return {
            "finished": True,
            "reason": reason,
            "best_attempt_id": self.db.scalar("SELECT best_attempt_id FROM tasks WHERE id=?", (task["id"],)),
            "note": "Task completed. Report only measured results; no improvement is a valid outcome.",
        }
