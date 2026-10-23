"""Deterministic parameter-experiment runner.

The runner is host-side: it enumerates configurations, materializes each one
from a fixed baseline source, snapshots the exact bytes, asks an evaluator for a
measurement, and records the result in ``evaluations``.  It never infers
success from an LLM hypothesis or from a comment-only edit.

The evaluator is injected so the same orchestration can be validated with
simulated results before it is connected to real scheduler submissions.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Callable

from .agent.change_audit import audit_candidate
from .config import Settings
from .db import Database
from .experiment_service import finish_experiment, note_result, set_working_revision
from .experiments import (
    SnapshotError,
    attach_snapshot,
    comparison_key,
    create_attempt,
    expected_testcases,
    get_attempt,
    normalize_result,
    read_snapshot,
    update_attempt,
)
from .util import canonical_json, json_dumps, json_loads, new_id, now


class ParameterRunnerError(RuntimeError):
    pass


class SchedulerEvaluator:
    """Real evaluator: queue one exact snapshot and wait for its remote result.

    The worker process performs the actual send/poll.  The runner must run in a
    separate management process and keep the task running; ``parameter_runner_active``
    prevents the Agent loop from creating competing attempts.
    """

    name = "cannjudge"

    def __init__(self, db: Database, scheduler: Any, task_id: str, *, timeout_sec: float = 1800, poll_sec: float = 2):
        self.db = db
        self.scheduler = scheduler
        self.task_id = task_id
        self.timeout_sec = timeout_sec
        self.poll_sec = poll_sec

    def __call__(self, attempt: dict, files: dict[str, str], params: dict, eval_key: str) -> dict:
        try:
            submission = self.scheduler.request_submission(self.task_id, attempt["id"])
        except Exception as exc:
            raise ParameterRunnerError(f"request_submission failed: {exc}") from exc
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            row = self.db.query_one("SELECT * FROM submissions WHERE id=?", (submission["id"],))
            if row and row["state"] == "finished":
                return json_loads(row.get("response_json"), {}) or {"status": "Fail", "msg": "empty result"}
            if row and row["state"] in ("failed", "unknown"):
                raw = json_loads(row.get("response_json"), {}) or {}
                raw.setdefault("status", "Fail")
                raw.setdefault("msg", row.get("error_message") or row["state"])
                return raw
            time.sleep(self.poll_sec)
        raise ParameterRunnerError(f"evaluation timed out after {self.timeout_sec}s")


DEFAULT_BINDINGS = {
    "tile_bytes": (
        r"constexpr\s+int64_t\s+kTileBytes\s*=\s*\d+\s*;",
        "constexpr int64_t kTileBytes = {value};",
    ),
    "depth": (
        r"constexpr\s+int64_t\s+kDepth\s*=\s*\d+\s*;",
        "constexpr int64_t kDepth = {value};",
    ),
}


def _config_params(config: dict) -> dict:
    params = config.get("params")
    if isinstance(params, dict):
        return dict(params)
    return {
        key: value
        for key, value in config.items()
        if key not in ("id", "role", "name", "description", "notes")
    }


class SimulatedParameterEvaluator:
    """Deterministic evaluator used by CLI smoke tests, never by production."""

    name = "simulated"

    def __call__(self, attempt: dict, files: dict[str, str], params: dict, eval_key: str) -> dict:
        tile = int(params.get("tile_bytes", 8192))
        depth = int(params.get("depth", 1))
        value = 1000.0 - (0 if tile == 8192 else 200) - (0 if depth == 1 else 120)
        return {
            "status": "Pass",
            "result": [{"testcase_status": "Pass", "time": value}],
            "score": 1,
        }


class ParameterRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        task_id: str,
        experiment_id: str,
        evaluator: Callable[[dict, dict[str, str], dict, str], dict],
        *,
        max_repairs_per_config: int = 1,
    ):
        self.db = db
        self.settings = settings
        self.task_id = task_id
        self.experiment_id = experiment_id
        self.evaluator = evaluator
        self.max_repairs_per_config = max_repairs_per_config
        self.task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not self.task:
            raise ParameterRunnerError(f"task not found: {task_id}")
        self.experiment = db.query_one("SELECT * FROM experiments WHERE id=?", (experiment_id,))
        if not self.experiment:
            raise ParameterRunnerError(f"experiment not found: {experiment_id}")
        self.problem = json_loads(self.task.get("problem_json"), {})
        self.config = json_loads(self.task.get("config_json"), {})
        self.plan = json_loads(self.experiment.get("search_space_json"), {})
        self.budget = json_loads(self.experiment.get("budget_json"), {})
        self.baseline_id = self.experiment.get("baseline_attempt_id")
        if not self.baseline_id:
            raise ParameterRunnerError("experiment has no fixed baseline")
        self.baseline = get_attempt(db, self.baseline_id)
        if not self.baseline or not self.baseline.get("snapshot_id"):
            raise ParameterRunnerError("baseline has no source snapshot")
        self.baseline_files = read_snapshot(db, settings, self.baseline["snapshot_id"])["files"]
        self.baseline_sha = db.scalar("SELECT sha256 FROM snapshots WHERE id=?", (self.baseline["snapshot_id"],))
        self.expected = expected_testcases(self.problem)
        self.fingerprint = comparison_key(self.problem, self.config)
        self.max_revisions = int(self.budget.get("max_revisions") or 0) or None
        self.max_evaluations = int(self.budget.get("max_evaluations") or 0) or None
        self.revisions = 0
        self.evaluations = 0
        self.evaluator_calls = 0
        self.results: list[dict] = []
        self.stop_reason: str | None = None

    # -- configuration materialization ---------------------------------
    def configs(self) -> list[dict]:
        raw = self.plan.get("configs")
        if not isinstance(raw, list) or not raw:
            raise ParameterRunnerError("search_space.configs must be a non-empty list")
        configs = []
        seen = set()
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise ParameterRunnerError("each config must be an object")
            config_id = str(item.get("id") or f"config-{index}")
            if config_id in seen:
                raise ParameterRunnerError(f"duplicate config id: {config_id}")
            seen.add(config_id)
            params = _config_params(item)
            if not params:
                raise ParameterRunnerError(f"config {config_id} has no parameters")
            configs.append({"id": config_id, "params": params, "raw": item})
        return configs

    def _bindings(self) -> dict:
        raw = self.plan.get("parameterization") or {}
        bindings = {}
        for name, spec in {**DEFAULT_BINDINGS, **(raw.get("bindings") or {})}.items():
            if isinstance(spec, dict):
                pattern = spec.get("pattern")
                template = spec.get("template")
            else:
                pattern, template = spec
            if pattern and template:
                bindings[name] = (str(pattern), str(template))
        return bindings

    def materialize(self, config: dict) -> tuple[dict[str, str], dict]:
        bindings = self._bindings()
        files = dict(self.baseline_files)
        changed = []
        for name, value in config["params"].items():
            if name not in bindings:
                raise ParameterRunnerError(f"unknown parameter {name!r} for config {config['id']}")
            pattern, template = bindings[name]
            replacement = template.format(value=value)
            hits = 0
            for path, content in list(files.items()):
                new_content, count = re.subn(pattern, replacement, content)
                if count:
                    files[path] = new_content
                    hits += count
                    changed.append({"path": path, "parameter": name, "value": value, "count": count})
            if hits == 0:
                raise ParameterRunnerError(
                    f"parameter {name!r} did not match the fixed baseline source"
                )
        audit = audit_candidate(self.baseline_files, files)
        # A configuration equal to the fixed baseline (for example D when the
        # baseline already has tile=32KB/depth=2) is a valid control.  It still
        # gets its own attempt/config/result record; it is not silently skipped.
        baseline_config = not audit["effective"]
        # Fixed-implementation guard: every changed line must be a parameter
        # binding.  A runner must never silently introduce another edit.
        changed_lines = [
            line for line in audit["diff_text"].splitlines()
            if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---"))
        ]
        allowed = [re.compile(pattern) for pattern, _ in bindings.values()]
        for line in changed_lines:
            text = line[1:].strip()
            if not text:
                continue
            if not any(pattern.search(text) for pattern in allowed):
                raise ParameterRunnerError(
                    f"fixed implementation violation in {config['id']}: unexpected diff line {text!r}"
                )
        return files, {"changed": changed, "audit": audit, "baseline_config": baseline_config}

    # -- evaluation records --------------------------------------------
    def _eval_key(self, config: dict, files: dict[str, str], kind: str = "parameter") -> str:
        payload = {
            "comparison_key": self.fingerprint,
            "baseline_sha": self.baseline_sha,
            "params": config["params"],
            "source_sha": self._source_sha(files),
            "kind": kind,
            "evaluator": getattr(self.evaluator, "name", "injected"),
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _source_sha(files: dict[str, str]) -> str:
        return hashlib.sha256(
            canonical_json({path: hashlib.sha256(content.encode("utf-8")).hexdigest() for path, content in sorted(files.items())}).encode("utf-8")
        ).hexdigest()

    def _attempt(self, config: dict, files: dict[str, str], kind: str) -> dict:
        attempt = create_attempt(
            self.db,
            self.task_id,
            parent_attempt_id=self.baseline_id,
            experiment_id=self.experiment_id,
            provider="parameter-runner",
            model="deterministic",
            prompt_version="parameter-runner-v1",
        )
        update_attempt(
            self.db,
            attempt["id"],
            revision_kind=kind,
            config_json=canonical_json(config["params"]),
            hypothesis=f"parameter config {config['id']}",
        )
        snapshot = attach_snapshot(
            self.db,
            self.settings,
            task_id=self.task_id,
            attempt_id=attempt["id"],
            files=files,
        )
        update_attempt(self.db, attempt["id"], snapshot_id=snapshot["id"])
        self.revisions += 1
        return get_attempt(self.db, attempt["id"])

    def _record(
        self,
        attempt: dict,
        config: dict,
        normalized: dict,
        *,
        kind: str,
        eval_key: str,
        reused: bool,
        replicate_of: str | None = None,
        raw: dict | None = None,
    ) -> dict:
        evaluation_id = new_id("eval")
        self.db.insert(
            "INSERT INTO evaluations(id, experiment_id, task_id, attempt_id, kind, config_json, "
            "environment_json, result_json, eval_key, reused, replicate_of, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evaluation_id,
                self.experiment_id,
                self.task_id,
                attempt["id"],
                kind,
                canonical_json(config["params"]),
                canonical_json({"comparison_key": self.fingerprint, "evaluator": getattr(self.evaluator, "name", "injected")}),
                canonical_json({"normalized": normalized, "raw": raw or {}}),
                eval_key,
                1 if reused else 0,
                replicate_of,
                now(),
            ),
        )
        result = {
            "attempt_id": attempt["id"],
            "evaluation_id": evaluation_id,
            "config_id": config["id"],
            "params": config["params"],
            "kind": kind,
            "eval_key": eval_key,
            "reused": reused,
            "replicate_of": replicate_of,
            "all_passed": bool(normalized.get("all_passed")),
            "testset_passed": bool(normalized.get("testset_passed")),
            "outcome": normalized.get("outcome"),
            "testcase_passed": normalized.get("testcase_passed"),
            "testcase_total": normalized.get("testcase_total"),
            "time_values": normalized.get("time_values") or [],
            "result": normalized,
        }
        self.results.append(result)
        self.db.execute(
            "UPDATE attempts SET status=?, result_json=?, normalized_json=?, finished_at=?, updated_at=? WHERE id=?",
            (
                "finished",
                canonical_json(raw or {}),
                canonical_json(normalized),
                now(),
                now(),
                attempt["id"],
            ),
        )
        return result

    def _existing(self, eval_key: str) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM evaluations WHERE task_id=? AND eval_key=? ORDER BY created_at ASC LIMIT 1",
            (self.task_id, eval_key),
        )

    def _evaluate(
        self,
        config: dict,
        files: dict[str, str],
        kind: str,
        *,
        force: bool = False,
        replicate_of: str | None = None,
    ) -> dict:
        eval_key = self._eval_key(config, files, kind)
        if not force and files == self.baseline_files:
            stored = json_loads(self.baseline.get("normalized_json"), {})
            if stored.get("all_passed"):
                attempt = self._attempt(config, files, kind)
                return self._record(
                    attempt,
                    config,
                    stored,
                    kind=kind,
                    eval_key=eval_key,
                    reused=True,
                    replicate_of=self.baseline_id,
                    raw=json_loads(self.baseline.get("result_json"), {}),
                )
        if not force:
            existing = self._existing(eval_key)
            if existing:
                attempt = self._attempt(config, files, kind)
                stored = json_loads(existing["result_json"], {})
                normalized = stored.get("normalized") or stored
                return self._record(
                    attempt,
                    config,
                    normalized,
                    kind=kind,
                    eval_key=eval_key,
                    reused=True,
                    replicate_of=existing["id"],
                    raw=stored.get("raw") or {},
                )
        if self.max_evaluations is not None and self.evaluations >= self.max_evaluations:
            raise ParameterRunnerError("evaluation budget exhausted")
        attempt = self._attempt(config, files, kind)
        self.evaluator_calls += 1
        raw = self.evaluator(attempt, files, config["params"], eval_key)
        normalized = normalize_result(raw, self.expected, self.fingerprint)
        self.evaluations += 1
        return self._record(
            attempt,
            config,
            normalized,
            kind=kind,
            eval_key=eval_key,
            reused=False,
            replicate_of=replicate_of,
            raw=raw,
        )

    def _apply_retest_policy(self) -> dict:
        """Retest configs whose measurements are close to the sweep's best."""
        criteria = json_loads(self.experiment.get("success_criteria_json"), {})
        threshold = criteria.get("repeat_if_within_pct")
        if threshold is None:
            return {"threshold_pct": None, "retested": [], "missing": []}
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            return {"threshold_pct": None, "retested": [], "missing": []}
        primary = [
            result
            for result in self.results
            if result.get("kind") == "parameter" and result.get("all_passed") and result.get("time_values")
        ]
        if not primary:
            return {"threshold_pct": threshold, "retested": [], "missing": []}
        best = min(max(result["time_values"]) for result in primary)
        needed = []
        for result in primary:
            value = max(result["time_values"])
            delta = abs(value - best) / best * 100 if best else 0
            if delta <= threshold:
                needed.append(result)
        retested = []
        missing = []
        for result in needed:
            config_id = result["config_id"]
            already = any(
                item.get("kind") == "retest" and item.get("config_id") == config_id
                for item in self.results
            )
            if already:
                retested.append(config_id)
                continue
            if self.max_revisions is not None and self.revisions >= self.max_revisions:
                self.stop_reason = "budget_exhausted"
                missing.append(config_id)
                continue
            attempt = get_attempt(self.db, result["attempt_id"])
            if not attempt or not attempt.get("snapshot_id"):
                missing.append(config_id)
                continue
            try:
                files = read_snapshot(self.db, self.settings, attempt["snapshot_id"])["files"]
                config = {"id": config_id, "params": result["params"], "raw": {}}
                retest = self._evaluate(
                    config,
                    files,
                    "retest",
                    force=True,
                    replicate_of=result["evaluation_id"],
                )
                if retest["all_passed"]:
                    retested.append(config_id)
                else:
                    missing.append(config_id)
            except (ParameterRunnerError, SnapshotError):
                missing.append(config_id)
        return {"threshold_pct": threshold, "retested": retested, "missing": missing}

    def _choose_best_result(self) -> dict | None:
        candidates = []
        for result in self.results:
            values = [float(value) for value in (result.get("time_values") or []) if value is not None]
            if result.get("all_passed") and values:
                candidates.append((max(values), result))
        return min(candidates, key=lambda item: item[0])[1] if candidates else None

    # -- orchestration -------------------------------------------------
    def run(self) -> dict:
        configs = self.configs()
        retest_configs = {str(value) for value in (self.plan.get("retest") or [])}
        required_retests = set(retest_configs)
        completed = set()
        failed = set()
        for config in configs:
            if self.max_revisions is not None and self.revisions >= self.max_revisions:
                self.stop_reason = "budget_exhausted"
                break
            try:
                files, _audit = self.materialize(config)
                result = self._evaluate(config, files, "parameter")
                repair_count = 0
                while not result["all_passed"] and repair_count < self.max_repairs_per_config:
                    if self.max_revisions is not None and self.revisions >= self.max_revisions:
                        self.stop_reason = "budget_exhausted"
                        break
                    repair_count += 1
                    result = self._evaluate(config, files, "parameter_repair")
                if result["all_passed"]:
                    completed.add(config["id"])
                    failed.discard(config["id"])
                    if result["attempt_id"]:
                        improved = note_result(self.db, self.experiment_id, result["attempt_id"], result["result"])
                        if improved:
                            set_working_revision(self.db, self.experiment_id, result["attempt_id"])
                else:
                    failed.add(config["id"])
                if self.stop_reason:
                    break
                if config["id"] in retest_configs:
                    if self.max_revisions is not None and self.revisions >= self.max_revisions:
                        self.stop_reason = "budget_exhausted"
                        break
                    retest = self._evaluate(
                        config,
                        files,
                        "retest",
                        force=True,
                        replicate_of=result["evaluation_id"],
                    )
                    if retest["all_passed"]:
                        completed.add(config["id"])
                    else:
                        failed.add(config["id"])
                    required_retests.discard(config["id"])
            except (ParameterRunnerError, SnapshotError) as exc:
                failed.add(config["id"])
                self.results.append({"config_id": config["id"], "error": str(exc), "all_passed": False})
                if self.max_revisions is not None and self.revisions >= self.max_revisions:
                    self.stop_reason = "budget_exhausted"
                    break

        retest_policy = {}
        if not self.stop_reason:
            retest_policy = self._apply_retest_policy()
            if retest_policy.get("missing"):
                required_retests.update(retest_policy["missing"])
        if not self.stop_reason:
            if required_retests:
                self.stop_reason = "retest_incomplete"
            elif failed:
                self.stop_reason = "config_failed"
        best_result = self._choose_best_result()
        experiment_best_id = best_result["attempt_id"] if best_result else None
        if experiment_best_id:
            self.db.execute(
                "UPDATE experiments SET experiment_best_id=?, updated_at=? WHERE id=?",
                (experiment_best_id, now(), self.experiment_id),
            )
            set_working_revision(self.db, self.experiment_id, experiment_best_id)
        verification_complete = (
            self.stop_reason is None
            and not failed
            and not required_retests
            and completed == {config["id"] for config in configs}
        )
        conclusion = {
            "status": "completed" if verification_complete else "incomplete",
            "verified": verification_complete,
            "completed_configs": sorted(completed),
            "failed_configs": sorted(failed),
            "retests_missing": sorted(required_retests),
            "stop_reason": self.stop_reason,
            "evaluations": self.evaluations,
            "evaluator_calls": self.evaluator_calls,
            "experiment_best_attempt_id": experiment_best_id,
            "retest_policy": retest_policy,
        }
        finish_experiment(
            self.db,
            self.experiment_id,
            status="completed" if verification_complete else "abandoned",
            conclusion=conclusion,
            stop_reason=self.stop_reason or ("completed" if verification_complete else "verification_incomplete"),
        )
        return conclusion
