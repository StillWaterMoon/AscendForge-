"""Agent Runtime: context assembly, model call, action validation, tool dispatch.

The runtime is deliberately single-step: the worker calls ``step`` repeatedly and
each call performs at most one model turn.  Long HTTP requests never own a task;
the worker process can crash and resume from persisted state.

Only actions, short hypotheses and tool results are persisted.  Hidden model
reasoning is neither requested nor stored.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..db import Database
from ..execution.capabilities import detect_capabilities
from ..experiments import (
    SnapshotError,
    add_event,
    attempt_history,
    create_attempt,
    current_attempt,
    get_attempt,
    read_snapshot,
    record_model_call,
    update_attempt,
)
from ..platform.service import PlatformService
from ..scheduler import Scheduler
from ..security import CredentialStore
from ..states import (
    ATTEMPT_BLOCKING_AGENT,
    ATTEMPT_FAILED,
    ATTEMPT_TERMINAL,
    TASK_RUNNING,
)
from ..tasks import TaskService
from ..util import canonical_json, json_dumps, json_loads, now, truncate_text
from .prompts import context_prompt, system_prompt
from .protocol import (
    TOOLS,
    Action,
    ActionError,
    action_from_text,
    validate_action,
)
from .providers import ModelConfig, ProviderError, ProviderResponse, make_provider
from .skills import SkillError, SkillRegistry
from .tools import ToolExecutor

log = logging.getLogger(__name__)

SKILLS_FOR_OPTIMIZATION = ["optimize-operator", "optimize-ascend-operator", "generate-operator-tests"]
MAX_CONTEXT_CHARS = 120_000
MAX_STORED_MESSAGE_CHARS = 20_000


class RuntimeUnavailable(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AgentRuntime:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        platform: PlatformService,
        tasks: TaskService,
        scheduler: Scheduler,
        credentials: CredentialStore,
        *,
        skills: SkillRegistry | None = None,
        provider_factory: Callable[[ModelConfig], Any] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.platform = platform
        self.tasks = tasks
        self.scheduler = scheduler
        self.credentials = credentials
        self.skills = skills or SkillRegistry(settings.root / "skills")
        self.tool_executor = ToolExecutor(db, settings, platform, tasks, scheduler)
        self.provider_factory = provider_factory or self._default_provider
        self._system_cache: tuple[str, list[dict]] | None = None

    # ------------------------------------------------------------------
    # Setup / provider
    # ------------------------------------------------------------------
    def _default_provider(self, config: ModelConfig):
        mock_script = None
        if config.provider in ("mock", "scripted"):
            path = self.settings.data_dir / "mock_script.json"
            if path.is_file():
                mock_script = json_loads(path.read_text(encoding="utf-8"), [])
        return make_provider(config, mock_script=mock_script)

    def model_config(self) -> ModelConfig:
        payload = self.credentials.get("model")
        if not payload:
            raise RuntimeUnavailable("MODEL_NOT_CONFIGURED", "模型 Provider 未配置")
        return ModelConfig.from_payload(payload)

    def system_prompt(self) -> tuple[str, list[dict]]:
        if self._system_cache is not None:
            return self._system_cache
        try:
            block, meta = self.skills.prompt_block(SKILLS_FOR_OPTIMIZATION)
        except SkillError as exc:
            log.error("skill loading failed: %s", exc)
            block = (
                "WARNING: the host could not load the required skills "
                f"({exc}). Follow the problem contract and never fabricate results."
            )
            meta = []
        prompt = system_prompt(block)
        self._system_cache = (prompt, meta)
        return self._system_cache

    # ------------------------------------------------------------------
    # Main step
    # ------------------------------------------------------------------
    def step(self, task_id: str) -> str:
        task = self.tasks.get(task_id)
        if task["status"] != TASK_RUNNING:
            return "not_running"
        allowed = self.scheduler.model_allowed(task)
        if not allowed.ok:
            self._handle_budget_stop(task, allowed)
            return f"budget:{allowed.reason}"
        try:
            config = self.model_config()
        except RuntimeUnavailable as exc:
            self.tasks.mark_needs_review(
                task_id, f"{exc.message}；请配置模型 API 后恢复任务", code=exc.code
            )
            return "needs_model"
        attempt = self._select_attempt(task, config)
        if attempt is None:
            return "no_attempt"
        if attempt["status"] in ATTEMPT_BLOCKING_AGENT:
            return "waiting"
        if attempt["status"] in ATTEMPT_TERMINAL:
            return "attempt_terminal"
        if int(attempt.get("step_count") or 0) >= int(self._config(task).get("agent_max_steps") or 10):
            self._fail_attempt(
                task,
                attempt,
                "AGENT_STEP_LIMIT",
                "agent reached max steps before requesting submission"
                if not attempt.get("snapshot_id")
                else "agent reached max steps after creating a candidate but did not request submission",
            )
            return "step_limit"
        if not self._skills_present(task, attempt):
            return "skills_missing"

        system, _ = self.system_prompt()
        messages = self._build_messages(task, attempt, system)
        try:
            provider = self.provider_factory(config)
            response = provider.complete(messages, tools=True)
        except ProviderError as exc:
            self._handle_provider_error(task, attempt, exc)
            return f"provider_error:{exc.code}"
        except Exception as exc:
            log.exception("unexpected provider failure")
            self.tasks.set_next_action(task_id, now() + 30, error_code="MODEL_FAILURE", error=str(exc)[:500])
            return "provider_unexpected"

        self._record_usage(task, attempt, config, response)
        try:
            actions = self._parse_response(response, task_id=task_id, attempt_id=attempt["id"])
        except ActionError as exc:
            self._invalid_action(task, attempt, response, exc)
            return "invalid_action"
        if not actions:
            self._invalid_action(
                task,
                attempt,
                response,
                ActionError("NO_ACTION", "model returned neither a tool call nor a JSON action"),
            )
            return "no_action"

        update_attempt(self.db, attempt["id"], step_count=int(attempt.get("step_count") or 0) + 1)
        native = bool(response.tool_calls)
        if native:
            calls = []
            for index, call in enumerate(response.tool_calls):
                arguments = call.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json_dumps(arguments if arguments is not None else {})
                calls.append(
                    {
                        "id": call.get("id") or f"call-{index}",
                        "type": "function",
                        "function": {"name": call.get("name"), "arguments": arguments},
                    }
                )
            self._append_message(
                attempt["id"],
                "assistant",
                json_dumps(
                    {
                        "_native_tool_calls": calls,
                        "content": truncate_text(response.content or "", 2000),
                    }
                ),
            )
        else:
            self._append_message(
                attempt["id"],
                "assistant",
                json_dumps(
                    {
                        "actions": [action.public() for action in actions],
                        "text": truncate_text(response.content or "", 2000),
                    }
                ),
            )
        blocked = False
        for index, action in enumerate(actions):
            result = self.tool_executor.execute(action, task_id=task_id, attempt_id=attempt["id"])
            if native:
                # Keep a valid OpenAI-native transcript: assistant tool_calls
                # followed by role=tool with the matching tool_call_id.
                call_id = response.tool_calls[index].get("id") or f"call-{index}"
                self._append_message(
                    attempt["id"],
                    "tool",
                    json_dumps(
                        {
                            "_tool_call_id": call_id,
                            "_tool_name": action.tool_name,
                            # Keep the envelope valid JSON and its tool/assistant
                            # pairing intact; only the observation is clipped.
                            "content": self._clip_result_text(result),
                        }
                    ),
                )
            else:
                # JSON-action mode is not OpenAI native tool calling: a bare
                # role="tool" message has no tool_call_id and strict gateways
                # reject it.  Deliver the result as a user observation instead.
                self._append_message(
                    attempt["id"],
                    "user",
                    "TOOL RESULT\n"
                    + json_dumps(
                        {
                            "action_id": action.action_id,
                            "tool_name": action.tool_name,
                            "result": result,
                        }
                    ),
                )
            add_event(
                self.db,
                task_id,
                attempt["id"],
                "agent_action",
                {
                    "action_id": action.action_id,
                    "tool_name": action.tool_name,
                    "ok": result.get("ok"),
                    "error": (result.get("error") or {}).get("code") if not result.get("ok") else None,
                },
            )
            if action.tool_name == "finish_task" and result.get("ok"):
                return "finished"
            if action.tool_name == "request_submission" and result.get("ok"):
                blocked = True
                break
        if blocked:
            return "waiting"
        refreshed = get_attempt(self.db, attempt["id"])
        if refreshed and int(refreshed.get("step_count") or 0) >= int(self._config(task).get("agent_max_steps") or 10):
            if refreshed.get("snapshot_id"):
                self._fail_attempt(
                    task,
                    refreshed,
                    "AGENT_STEP_LIMIT",
                    "agent reached max steps after creating a candidate but did not request submission; snapshot retained for inspection",
                )
            else:
                self._fail_attempt(
                    task,
                    refreshed,
                    "AGENT_STEP_LIMIT",
                    "agent reached max steps without creating a candidate",
                )
            return "step_limit"
        return "continue"

    # ------------------------------------------------------------------
    # Attempt selection
    # ------------------------------------------------------------------
    def _config(self, task: dict) -> dict:
        config = task.get("config") or task.get("config_json") or {}
        if isinstance(config, str):
            config = json_loads(config, {})
        return config

    def _select_attempt(self, task: dict, config: ModelConfig) -> dict | None:
        latest = current_attempt(self.db, task["id"])
        if latest and latest["status"] in ATTEMPT_BLOCKING_AGENT:
            return latest
        if latest and latest["status"] in ATTEMPT_TERMINAL:
            parent = task.get("best_attempt_id") or latest["id"]
            return create_attempt(
                self.db,
                task["id"],
                parent_attempt_id=parent,
                provider=config.provider,
                model=config.model,
                prompt_version=self._prompt_version(),
            )
        if latest and latest["status"] in ("generated", "local_checking"):
            return latest
        if latest is None:
            return create_attempt(
                self.db,
                task["id"],
                provider=config.provider,
                model=config.model,
                prompt_version=self._prompt_version(),
            )
        if latest["status"] == ATTEMPT_FAILED:
            return create_attempt(
                self.db,
                task["id"],
                parent_attempt_id=task.get("best_attempt_id") or latest["id"],
                provider=config.provider,
                model=config.model,
                prompt_version=self._prompt_version(),
            )
        return latest

    def _prompt_version(self) -> str:
        _, meta = self.system_prompt()
        digest = "".join(sorted(item["sha256"][:8] for item in meta)) or "noskills"
        return f"v1+{digest}"

    def _skills_present(self, task: dict, attempt: dict) -> bool:
        _, meta = self.system_prompt()
        if len(meta) == len(SKILLS_FOR_OPTIMIZATION):
            return True
        if not self.db.query_one(
            "SELECT 1 FROM events WHERE task_id=? AND type='skills_missing' LIMIT 1", (task["id"],)
        ):
            add_event(self.db, task["id"], attempt["id"], "skills_missing", {"expected": SKILLS_FOR_OPTIMIZATION})
            self.tasks.mark_needs_review(
                task["id"], "缺少必需的 skills 文本，无法按规范运行 Agent", code="SKILLS_MISSING"
            )
        return False

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def _load_messages(self, attempt_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT role, content FROM agent_messages WHERE attempt_id=? ORDER BY seq", (attempt_id,)
        )
        messages = []
        for row in rows:
            role = row["role"]
            content = row["content"]
            if role == "assistant" and content.startswith("{"):
                envelope = json_loads(content, None)
                if isinstance(envelope, dict) and "_native_tool_calls" in envelope:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": envelope.get("content") or "",
                            "tool_calls": envelope["_native_tool_calls"],
                        }
                    )
                    continue
            if role == "tool":
                envelope = json_loads(content, None)
                if isinstance(envelope, dict) and "_tool_call_id" in envelope:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": envelope["_tool_call_id"],
                            "name": envelope.get("_tool_name") or "",
                            "content": envelope.get("content") or "",
                        }
                    )
                    continue
                # legacy/invalid bare tool message: downgrade to a user
                # observation so strict gateways cannot reject it
                messages.append({"role": "user", "content": content})
                continue
            messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _clip_result_text(value: Any, limit: int = 12_000) -> str:
        text = json_dumps(value)
        if len(text) > limit:
            text = text[:limit] + "\n...[tool result truncated]..."
        return text

    def _append_message(self, attempt_id: str, role: str, content: str) -> None:
        seq = int(
            self.db.scalar(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_messages WHERE attempt_id=?", (attempt_id,)
            )
            or 1
        )
        self.db.insert(
            "INSERT INTO agent_messages(attempt_id, seq, role, content, created_at) VALUES(?, ?, ?, ?, ?)",
            (attempt_id, seq, role, truncate_text(content, MAX_STORED_MESSAGE_CHARS), now()),
        )

    def _build_messages(self, task: dict, attempt: dict, system: str) -> list[dict]:
        stored = self._load_messages(attempt["id"])
        if not stored:
            capabilities = detect_capabilities(self.settings)
            best_files = None
            if task.get("best_attempt_id"):
                best = get_attempt(self.db, task["best_attempt_id"])
                if best and best.get("snapshot_id"):
                    try:
                        best_files = read_snapshot(self.db, self.settings, best["snapshot_id"])["files"]
                    except SnapshotError:
                        best_files = None
            parent_files = None
            parent_attempt = None
            parent_id = attempt.get("parent_attempt_id") or task.get("best_attempt_id")
            if parent_id:
                candidate_parent = get_attempt(self.db, parent_id)
                if candidate_parent and candidate_parent.get("snapshot_id"):
                    try:
                        parent_files = read_snapshot(
                            self.db, self.settings, candidate_parent["snapshot_id"]
                        )["files"]
                        parent_attempt = candidate_parent
                    except SnapshotError:
                        parent_files = None
                        parent_attempt = None
            normalized_attempts = []
            for item in attempt_history(self.db, task["id"], limit=20):
                normalized_attempts.append(
                    {
                        **item,
                        "normalized": json_loads(item.get("normalized_json"), {}),
                    }
                )
            context = context_prompt(
                task=task,
                attempt=attempt,
                attempts=normalized_attempts,
                best_files=best_files,
                parent_files=parent_files,
                parent_attempt=parent_attempt,
                capabilities={
                    "npu": capabilities.get("npu"),
                    "candidate_execution": capabilities.get("candidate_execution"),
                    "executor": capabilities.get("executor"),
                    "remote_custom_tests": capabilities.get("remote_custom_tests"),
                },
            )
            self._append_message(attempt["id"], "user", context)
            stored = [{"role": "user", "content": context}]
        messages = [{"role": "system", "content": system}, *stored]
        return self._trim(messages)

    @staticmethod
    def _trim(messages: list[dict]) -> list[dict]:
        total = sum(len(str(item.get("content") or "")) for item in messages)
        if total <= MAX_CONTEXT_CHARS:
            return messages
        system = messages[0]
        rest = messages[1:]
        first_user = rest[0] if rest and rest[0].get("role") == "user" else None
        tails = rest[-8:]
        trimmed = [system]
        if first_user:
            trimmed.append(
                {"role": "user", "content": truncate_text(first_user["content"], 60_000)}
            )
        for item in tails:
            if first_user and item is first_user:
                continue
            trimmed.append(
                {"role": item.get("role", "user"), "content": truncate_text(str(item.get("content") or ""), 12_000)}
            )
        return trimmed

    # ------------------------------------------------------------------
    # Response parsing / invalid handling
    # ------------------------------------------------------------------
    def _parse_response(
        self, response: ProviderResponse, *, task_id: str, attempt_id: str
    ) -> list[Action]:
        if response.tool_calls:
            actions = []
            for index, call in enumerate(response.tool_calls):
                raw = {
                    "protocol_version": "1",
                    "action_id": call.get("id") or f"call-{index}",
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "tool_name": call.get("name"),
                    "arguments": call.get("arguments") or {},
                }
                actions.append(
                    validate_action(raw, task_id=task_id, attempt_id=attempt_id, tools=TOOLS)
                )
            return actions
        return [action_from_text(response.content, task_id=task_id, attempt_id=attempt_id)]

    def _invalid_action(self, task: dict, attempt: dict, response: ProviderResponse, exc: ActionError) -> None:
        add_event(
            self.db,
            task["id"],
            attempt["id"],
            "invalid_action",
            {"code": exc.code, "message": exc.message, "content": truncate_text(response.content or "", 1000)},
        )
        update_attempt(
            self.db,
            attempt["id"],
            error_code=exc.code,
            error_message=exc.message,
            step_count=int(attempt.get("step_count") or 0) + 1,
        )
        steps = int(attempt.get("step_count") or 0) + 1
        if steps >= int(self._config(task).get("agent_max_steps") or 10):
            self._fail_attempt(task, attempt, "INVALID_ACTION_LIMIT", "agent repeatedly returned invalid actions")
        else:
            self.tasks.set_next_action(task["id"], now() + 5)
        self._append_message(
            attempt["id"],
            "user",
            f"Your previous reply was not a valid action: {exc.code}: {exc.message}. "
            "Return exactly one JSON tool action using the documented schema.",
        )

    # ------------------------------------------------------------------
    # Provider errors / usage
    # ------------------------------------------------------------------
    def _handle_provider_error(self, task: dict, attempt: dict, exc: ProviderError) -> None:
        add_event(
            self.db,
            task["id"],
            attempt["id"],
            "model_error",
            {"code": exc.code, "message": exc.message[:500], "retryable": exc.retryable},
        )
        self.tasks.set_last_error(task["id"], exc.code, exc.message[:500])
        if exc.code in ("MODEL_AUTH", "MODEL_FORBIDDEN", "MODEL_NOT_CONFIGURED", "INSECURE_MODEL_URL", "BAD_MODEL_URL"):
            self.tasks.mark_needs_review(
                task["id"], f"模型 API 配置/鉴权失败：{exc.message}", code=exc.code
            )
        elif exc.retryable:
            self.tasks.set_next_action(task["id"], now() + (30 if exc.code == "MODEL_NETWORK" else 15))
        else:
            self._fail_attempt(task, attempt, exc.code, exc.message)
            self.tasks.set_next_action(task["id"], now() + 10)

    def _record_usage(self, task: dict, attempt: dict, config: ModelConfig, response: ProviderResponse) -> None:
        prompt = int(response.prompt_tokens or 0)
        completion = int(response.completion_tokens or 0)
        cost, known = config.cost(prompt, completion)
        record_model_call(
            self.db,
            task_id=task["id"],
            attempt_id=attempt["id"],
            provider=config.provider,
            model=response.model or config.model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_estimate=cost,
            cost_known=known,
            duration_ms=0,
            ok=True,
        )
        old_prompt = int(attempt.get("prompt_tokens") or 0)
        old_completion = int(attempt.get("completion_tokens") or 0)
        old_cost = attempt.get("cost_estimate")
        new_cost = None
        if known and cost is not None:
            new_cost = float(old_cost or 0) + cost
        elif old_cost is not None:
            new_cost = old_cost
        update_attempt(
            self.db,
            attempt["id"],
            prompt_tokens=old_prompt + prompt,
            completion_tokens=old_completion + completion,
            cost_estimate=new_cost,
            provider=config.provider,
            model=response.model or config.model,
        )

    # ------------------------------------------------------------------
    # Stops / failures
    # ------------------------------------------------------------------
    def _handle_budget_stop(self, task: dict, reservation) -> None:
        reason = reservation.reason
        if reason in ("max_attempts", "max_model_calls", "max_tokens", "max_cost", "max_runtime"):
            self.tasks.complete(task["id"], f"budget:{reason}")
        elif reason == "cost_unknown":
            self.tasks.mark_needs_review(
                task["id"],
                "费用上限已设置，但存在未配置单价的模型调用；未知费用不能当作 0。请补充单价或移除 max_cost。",
                code="COST_UNKNOWN",
            )
        else:
            self.tasks.set_next_action(task["id"], now() + 30)

    def _fail_attempt(self, task: dict, attempt: dict, code: str, message: str) -> None:
        update_attempt(
            self.db,
            attempt["id"],
            status=ATTEMPT_FAILED,
            error_code=code,
            error_message=message[:2000],
            finished_at=now(),
        )
        add_event(
            self.db,
            task["id"],
            attempt["id"],
            "attempt_failed",
            {"code": code, "message": message[:1000]},
        )
        self.tasks.set_last_error(task["id"], code, message[:500])
        self.tasks.set_next_action(task["id"], now())
        self.scheduler._evaluate_stop(task["id"])
