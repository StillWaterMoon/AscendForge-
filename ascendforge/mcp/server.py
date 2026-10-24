"""Minimal stdio MCP server for AscendForge.

Run one server per task/session:

    python -m ascendforge.mcp.server --task-id <task_id> --workspace <draft_dir>

The process speaks JSON-RPC 2.0 over stdin/stdout.  Protocol logs go to stderr
only, so they can never corrupt an MCP response.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from .contracts import TOOL_SPECS, ToolArgumentError, validate_arguments
from .functions import MCPToolError, MCPToolFunctions

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ascendforge", "version": "0.1.0"}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class MCPServer:
    def __init__(self, functions: MCPToolFunctions | None = None):
        self.functions = functions

    @staticmethod
    def _result(request_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle(self, message: dict) -> dict | None:
        if not isinstance(message, dict):
            return self._error(None, -32600, "invalid request")
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "notifications/cancelled":
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOL_SPECS})
        if method == "tools/call":
            if not isinstance(params, dict):
                return self._error(request_id, -32602, "tools/call params must be an object")
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                return self._error(request_id, -32602, "tool name is required")
            if self.functions is None:
                text = "this MCP server has no task context; start it with --task-id"
                return self._result(
                    request_id,
                    {"content": [{"type": "text", "text": text}], "isError": True},
                )
            try:
                validate_arguments(name, arguments)
                result = self.functions.call(name, arguments)
                return self._result(
                    request_id,
                    {"content": [{"type": "text", "text": _json_text(result)}], "isError": False},
                )
            except (ToolArgumentError, MCPToolError, ValueError) as exc:
                return self._result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                        "isError": True,
                    },
                )
            except Exception as exc:  # pragma: no cover - defensive boundary
                return self._result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": f"internal tool error: {type(exc).__name__}: {exc}"}],
                        "isError": True,
                    },
                )
        if method in ("shutdown", "exit"):
            return self._result(request_id, {})
        if is_notification:
            return None
        return self._error(request_id, -32601, f"method not found: {method}")


def _read_message(stream) -> dict | None:
    """Read one JSON-RPC message; support newline and Content-Length framing."""
    line = stream.readline()
    if not line:
        return None
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    if line.lower().startswith("content-length:"):
        length = int(line.split(":", 1)[1].strip())
        while True:
            header = stream.readline()
            if not header:
                return None
            if isinstance(header, bytes):
                header = header.decode("ascii", errors="replace")
            if header.strip() == "":
                break
        payload = stream.read(length)
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        return json.loads(payload)
    text = line.strip()
    if not text:
        return _read_message(stream)
    return json.loads(text)


def serve_stdio(functions: MCPToolFunctions | None) -> int:
    server = MCPServer(functions)
    for raw in sys.stdin:
        text = raw.strip()
        if not text:
            continue
        message = None
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            response = server._error(None, -32700, f"parse error: {exc}")
        else:
            response = server.handle(message)
        if response is not None:
            sys.stdout.write(_json_text(response) + "\n")
            sys.stdout.flush()
        if isinstance(message, dict) and message.get("method") == "exit":
            break
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AscendForge MCP stdio server")
    parser.add_argument("--task-id", required=False, help="task scope for tool calls")
    parser.add_argument("--workspace", help="CLI draft directory (defaults to the task workspace)")
    parser.add_argument("--data-dir", help="override ASCENDFORGE_DATA_DIR")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir:
        import os

        os.environ["ASCENDFORGE_DATA_DIR"] = args.data_dir
    settings = Settings.from_env()
    db = Database(settings.db_path)
    db.init()
    functions = None
    if args.task_id:
        functions = MCPToolFunctions(settings, db, args.task_id, workspace=args.workspace)
    return serve_stdio(functions)


if __name__ == "__main__":
    raise SystemExit(main())
