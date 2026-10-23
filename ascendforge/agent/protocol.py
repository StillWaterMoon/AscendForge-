"""Versioned structured action protocol.

Models without native tool calling return exactly one JSON object:

    {"protocol_version":"1","action_id":"...","task_id":"...","attempt_id":"...",
     "tool_name":"create_candidate","arguments":{...},"summary":"..."}

The host validates ownership, state and arguments before executing anything.
No model output is ever passed to a shell.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "1"


class ActionError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _schema(
    properties: dict,
    required: list[str] | None = None,
    *,
    additional: bool = False,
    description: str = "",
) -> dict:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": required or [],
        "additionalProperties": additional,
    }


TOOLS: dict[str, dict] = {
    "read_problem": {
        "name": "read_problem",
        "description": "Read the imported problem contract, template file list, editable contents and small read-only files (interface/dtype hints). Call this before writing code if the contract is unclear.",
        "parameters": _schema(
            {
                "include_contents": {
                    "type": "boolean",
                    "description": "Include editable template contents (default true).",
                },
                "include_readonly": {
                    "type": "boolean",
                    "description": "Include small read-only template files (default true; interface/dtype hints often live there).",
                },
            },
            description="Read problem metadata and template files.",
        ),
    },
    "read_history": {
        "name": "read_history",
        "description": "Read previous attempts, hypotheses, snapshots and normalized remote results. This returns metadata only; use read_attempt_source for code.",
        "parameters": _schema(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Number of recent attempts."}},
            description="Read experiment history.",
        ),
    },
    "read_attempt_source": {
        "name": "read_attempt_source",
        "description": "Read the complete source snapshot of a previous attempt (default: the parent of the current attempt). Use this to make a minimal patch instead of rewriting from the template.",
        "parameters": _schema(
            {
                "attempt_id": {
                    "type": "string",
                    "description": "Attempt id; omitted means the current attempt's parent.",
                }
            },
            description="Read a historical candidate source.",
        ),
    },
    "create_candidate": {
        "name": "create_candidate",
        "description": "Create an immutable candidate snapshot for the current attempt. For npu_kernel_dev submit every editable template file plus optional root-level .asc/.h helpers. Call request_submission afterwards.",
        "parameters": _schema(
            {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "items": _schema(
                        {
                            "path": {"type": "string", "minLength": 1, "maxLength": 255},
                            "content": {"type": "string"},
                        },
                        ["path", "content"],
                    ),
                },
                "hypothesis": {"type": "string", "minLength": 1, "maxLength": 4000},
                "parent_attempt_id": {"type": "string"},
            },
            ["files", "hypothesis"],
            description="Candidate source snapshot.",
        ),
    },
    "create_test_generator": {
        "name": "create_test_generator",
        "description": "Store a Python data generator and an independent reference implementation. They are executed only by run_local_validation in the isolated executor; creating them does not run them.",
        "parameters": _schema(
            {
                "generator_source": {"type": "string", "minLength": 1, "maxLength": 200000},
                "reference_source": {"type": "string", "minLength": 1, "maxLength": 200000},
                "seed": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                "spec": {
                    "type": "object",
                    "description": "Shapes, dtypes, value ranges, edge cases and tolerances actually generated.",
                },
                "validation": {
                    "type": "object",
                    "description": "Comparison rules (absolute/relative tolerance, NaN semantics, invariants).",
                },
            },
            ["generator_source", "reference_source", "seed"],
            description="Store generator and reference sources.",
        ),
    },
    "run_local_validation": {
        "name": "run_local_validation",
        "description": "Execute the stored generator and reference implementation on the server in the isolated executor. Without an NPU, the Ascend C candidate itself remains untested; a CPU reference pass is not an operator pass.",
        "parameters": _schema(
            {"generator_id": {"type": "string", "minLength": 1}},
            ["generator_id"],
            description="Run generator/reference validation.",
        ),
    },
    "request_submission": {
        "name": "request_submission",
        "description": "Ask the host scheduler to submit the current immutable snapshot to CANNJudge. This does not send immediately and cannot bypass quotas. Never call CANNJudge directly.",
        "parameters": _schema({}, description="Request a scheduled submission."),
    },
    "query_result": {
        "name": "query_result",
        "description": "Read the host's stored submission/result state. The scheduler performs remote polling; this tool does not create network traffic.",
        "parameters": _schema(
            {"submission_id": {"type": "string"}},
            description="Read stored submission state.",
        ),
    },
    "start_experiment": {
        "name": "start_experiment",
        "description": "Close the current experiment and start a new one with a fixed baseline, explicit hypothesis, search space and success criteria. Use this after the current direction has produced a conclusion; do not use it to hide a failed build.",
        "parameters": _schema(
            {
                "hypothesis": {"type": "string", "minLength": 1, "maxLength": 8000},
                "baseline_attempt_id": {"type": "string"},
                "search_space": {"type": "object"},
                "success_criteria": {"type": "object"},
                "budget": {"type": "object"},
            },
            ["hypothesis"],
            description="Start a new experiment.",
        ),
    },
    "finish_experiment": {
        "name": "finish_experiment",
        "description": "Close the active experiment and record a factual conclusion. The host records the evidence; the summary must not claim more than the measured result supports.",
        "parameters": _schema(
            {
                "status": {"type": "string", "enum": ["completed", "abandoned"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            ["status", "summary"],
            description="Finish the active experiment.",
        ),
    },
    "finish_task": {
        "name": "finish_task",
        "description": "Stop optimizing and finish the task. Use after budget, target, or when no further useful hypothesis remains. Do not claim a score improvement that was not measured.",
        "parameters": _schema(
            {
                "reason": {"type": "string", "enum": ["target_reached", "budget", "no_improvement", "blocked", "user_request", "other"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            ["reason", "summary"],
            description="Finish the task.",
        ),
    },
}


@dataclass(slots=True)
class Action:
    tool_name: str
    arguments: dict
    action_id: str
    task_id: str
    attempt_id: str
    protocol_version: str = PROTOCOL_VERSION
    summary: str = ""
    raw: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "protocol_version": self.protocol_version,
            "action_id": self.action_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "summary": self.summary,
        }


def _validate_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected and not _validate_type(value, expected):
        return [f"{path}: expected {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value not in enum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, item_schema, f"{path}[{index}]"))
    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}.{required}: required")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                errors.extend(validate_schema(item, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unexpected property")
    return errors


def validate_action(
    raw: dict,
    *,
    task_id: str,
    attempt_id: str,
    tools: dict[str, dict] | None = None,
) -> Action:
    if not isinstance(raw, dict):
        raise ActionError("BAD_ACTION", "action must be a JSON object")
    if "function" in raw and isinstance(raw["function"], dict):
        raw = {**raw, **raw["function"]}
    tool_name = raw.get("tool_name") or raw.get("tool") or raw.get("name")
    arguments = raw.get("arguments", raw.get("args", raw.get("parameters", {})))
    if not isinstance(tool_name, str) or not tool_name:
        raise ActionError("MISSING_TOOL", "action is missing tool_name")
    if tools is not None and tool_name not in tools:
        raise ActionError(
            "UNKNOWN_TOOL",
            f"unknown tool: {tool_name}; allowed: {', '.join(sorted(tools))}",
        )
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            raise ActionError("BAD_ARGUMENTS", "arguments must be a JSON object") from None
    if not isinstance(arguments, dict):
        raise ActionError("BAD_ARGUMENTS", "arguments must be a JSON object")
    if tools is not None:
        schema = tools[tool_name].get("parameters", {})
        errors = validate_schema(arguments, schema)
        if errors:
            raise ActionError("BAD_ARGUMENTS", "; ".join(errors[:8]))
    version = str(raw.get("protocol_version") or PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ActionError("PROTOCOL_VERSION", f"unsupported protocol_version: {version}")
    raw_task = raw.get("task_id")
    raw_attempt = raw.get("attempt_id")
    if raw_task and str(raw_task) != task_id:
        raise ActionError("TASK_MISMATCH", "action task_id does not match the active task")
    if raw_attempt and str(raw_attempt) != attempt_id:
        raise ActionError("ATTEMPT_MISMATCH", "action attempt_id does not match the active attempt")
    action_id = str(raw.get("action_id") or "").strip()
    if not action_id:
        action_id = "auto-" + __import__("secrets").token_hex(4)
    return Action(
        tool_name=tool_name,
        arguments=arguments,
        action_id=action_id,
        task_id=task_id,
        attempt_id=attempt_id,
        protocol_version=version,
        summary=str(raw.get("summary") or "")[:1000],
        raw=raw,
    )


def extract_json_object(text: str) -> dict | None:
    """Extract the first JSON object from model text, tolerating fences."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(cleaned[start : index + 1])
                    return value if isinstance(value, dict) else None
                except ValueError:
                    return None
    return None


def action_from_text(text: str, *, task_id: str, attempt_id: str) -> Action:
    payload = extract_json_object(text)
    if payload is None:
        raise ActionError(
            "NO_ACTION",
            "response did not contain a JSON action object; return exactly one tool action",
        )
    if "action" in payload and isinstance(payload["action"], dict):
        payload = payload["action"]
    return validate_action(payload, task_id=task_id, attempt_id=attempt_id, tools=TOOLS)


def tool_result_ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def tool_result_error(code: str, message: str, *, retryable: bool = False) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}
