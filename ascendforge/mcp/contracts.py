"""MCP 工具契约：定义 Codex 可以调用的六个只读/提交函数。

设计原则：
- 读取和提交彻底分开，避免把“已接收”误当成“已通过”；
- 提交只返回 ID，结果必须用同一个 ID 再查询；
- 不提供代码编辑工具，源码由 Codex 自己维护；
- 参数在这里集中校验，任何工具实现都不能绕过。

六个工具：
  read_problem / read_problem_file
  read_server_evaluation / read_official_evaluation
  submit_server_test / submit_official
"""

from __future__ import annotations

from typing import Any

MAX_FILE_BYTES = 512 * 1024

# 这组 schema 同时用于 MCP tools/list 和本地参数校验。
# 描述保持英文，方便 Codex 直接理解；注释和文档使用中文。
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "read_problem",
        "description": (
            "Read the imported problem: statement, interface/precision constraints, "
            "file directory, editable scope, template file list and current attempt ids."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_statement": {
                    "type": "boolean",
                    "description": "Include the full problem statement (default true).",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_problem_file",
        "description": (
            "Read a problem source file. Typical paths: kernel.asc, main.asc, "
            "reference.py, sample.py, CMakeLists.txt, run.sh. Read-only files are "
            "returned without modification. Paths are restricted to the task files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 255},
                "source": {
                    "type": "string",
                    "enum": ["workspace", "snapshot", "template"],
                    "description": "Where to read from; default: current workspace, then latest snapshot/template.",
                },
                "attempt_id": {
                    "type": "string",
                    "description": "Snapshot attempt id when source=snapshot; default latest candidate.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_server_evaluation",
        "description": (
            "Read a fixed-test NPU server evaluation by evaluation_id. Returns queue/running "
            "status, compile logs, correctness coverage and wall-clock timing. This is a "
            "server test, not an official OJ result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "evaluation_id": {"type": "string", "minLength": 1},
                "include_logs": {"type": "boolean"},
            },
            "required": ["evaluation_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_official_evaluation",
        "description": (
            "Read an official CANNJudge submission by submission_id. Returns queue/judging "
            "state, compile errors, per-testcase results, timing, score and source hash."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "submission_id": {"type": "string", "minLength": 1},
                "include_raw": {"type": "boolean"},
            },
            "required": ["submission_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_server_test",
        "description": (
            "Snapshot the current draft and queue a fixed-test NPU server evaluation. "
            "Returns evaluation_id and source sha256 immediately; query the result with "
            "read_server_evaluation. This does not consume official OJ quota."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Optional idempotency key; retries with the same value return the same evaluation.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_official",
        "description": (
            "Snapshot the current draft and request an official CANNJudge submission through "
            "the host scheduler. Returns submission_id and source sha256. Frequency/quota/"
            "in-flight limits still apply. Query with read_official_evaluation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Optional idempotency key; retries with the same value do not create another submission.",
                },
                "parent_attempt_id": {
                    "type": "string",
                    "description": "Optional parent attempt; default latest attempt with a snapshot.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = tuple(spec["name"] for spec in TOOL_SPECS)
TOOLS_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}


class ToolArgumentError(ValueError):
    pass


def validate_arguments(name: str, arguments: dict | None) -> dict:
    """校验参数后返回一份浅拷贝。

    这里故意不引入 jsonschema 依赖：工具参数都很小，手写校验更容易
    保证错误信息可读，也避免模型传入嵌套结构绕过限制。
    """
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise ToolArgumentError(f"unknown tool: {name}")
    args = {} if arguments is None else arguments
    if not isinstance(args, dict):
        raise ToolArgumentError("arguments must be an object")
    schema = spec["inputSchema"]
    properties = schema.get("properties") or {}
    if not schema.get("additionalProperties", False):
        unknown = sorted(set(args) - set(properties))
        if unknown:
            raise ToolArgumentError(f"unknown argument(s): {', '.join(unknown)}")
    for required in schema.get("required") or []:
        if required not in args:
            raise ToolArgumentError(f"missing required argument: {required}")
    for key, value in args.items():
        rule = properties.get(key) or {}
        expected = rule.get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolArgumentError(f"{key} must be a string")
        if expected == "boolean" and not isinstance(value, bool):
            raise ToolArgumentError(f"{key} must be a boolean")
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ToolArgumentError(f"{key} must be an integer")
        if isinstance(value, str):
            if rule.get("minLength") is not None and len(value) < rule["minLength"]:
                raise ToolArgumentError(f"{key} is too short")
            if rule.get("maxLength") is not None and len(value) > rule["maxLength"]:
                raise ToolArgumentError(f"{key} is too long")
        if rule.get("enum") and value not in rule["enum"]:
            raise ToolArgumentError(f"{key} must be one of {rule['enum']}")
    return dict(args)
