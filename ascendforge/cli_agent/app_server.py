"""Codex app-server transport for visible per-task Codex sessions.

A real app-server thread is created in the user's Codex session store; the
same thread is resumed for later turns.  This keeps the conversation visible to
Codex clients while the host still owns snapshotting and official submission.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .codex import CliEvent, CodexRunResult, parse_jsonl
from .probe import probe_codex


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class AppServerAdapter:
    name = "codex-app-server"

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_sec: float = 1800,
        on_event=None,
        on_tool_call=None,
        tools: list[dict] | None = None,
    ):
        probe = probe_codex()
        self.executable = executable or probe.get("executable") or "codex.cmd"
        self.timeout_sec = timeout_sec
        self.on_event = on_event
        self.on_tool_call = on_tool_call
        self.tools = tools or []
        self.node = shutil.which("node") or "node"
        self.port = _free_port()
        self.server = subprocess.Popen(
            [self.executable, "app-server", "--listen", f"ws://127.0.0.1:{self.port}"],
            # app-server treats stdin EOF as a shutdown signal; keep it open.
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # The server binds quickly; a short retry is cheaper than guessing.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.15)
        detail = ""
        try:
            if self.server.poll() is None:
                self.server.terminate()
                self.server.wait(timeout=3)
            if self.server.stderr:
                detail = self.server.stderr.read(2000)
        except Exception:
            pass
        self.close()
        raise RuntimeError(f"Codex app-server did not start: {detail[-1000:]}")

    def close(self) -> None:
        if getattr(self, "server", None) and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()

    def prepare_workspace(self, directory: Path, files: dict[str, str], **kwargs) -> Path:
        # Reuse the file-layout behavior without instantiating a second adapter.
        workspace = Path(directory) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (workspace / "TASK.json").write_text(
            json.dumps(kwargs.get("task_meta") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (workspace / "AGENTS.md").write_text(
            "# Task workspace\n\n"
            "Only modify source files under this workspace. Do not access credentials, "
            "the platform database, or submit to the official judge.\n\n"
            + str(kwargs.get("instructions") or ""),
            encoding="utf-8",
        )
        return workspace

    def collect_changes(self, workspace: Path, baseline: dict[str, str]) -> dict[str, str]:
        changes = {}
        for rel, old in baseline.items():
            path = Path(workspace) / rel
            if path.exists():
                new = path.read_text(encoding="utf-8")
                if new != old:
                    changes[rel] = new
        return changes

    def _turn(self, prompt: str, workspace: Path, thread: str | None, output_file: Path) -> CodexRunResult:
        script = Path(__file__).with_name("app_server_turn.mjs")
        tools_file = Path(output_file).parent / "codex-tools.json"
        tools_file.write_text(json.dumps(self.tools, ensure_ascii=False), encoding="utf-8")
        prompt_file = Path(output_file).parent / "codex-prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        command = [
            self.node,
            str(script),
            f"port={self.port}",
            f"cwd={workspace}",
            f"prompt_file={prompt_file}",
            f"thread={thread or ''}",
            f"tools_file={tools_file}",
            f"timeout_ms={int(self.timeout_sec * 1000)}",
        ]
        started = time.monotonic()
        run_id = f"appserver-{int(time.time() * 1000)}"
        events: list[CliEvent] = []
        timed_out = False
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            bufsize=1,
        )
        deadline = started + self.timeout_sec
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if time.monotonic() > deadline:
                    timed_out = True
                    process.kill()
                    break
                parsed = parse_jsonl(line)
                for event in parsed:
                    events.append(event)
                    if self.on_event:
                        try:
                            self.on_event(event)
                        except Exception:
                            pass
                    if event.type == "tool_call" and self.on_tool_call and process.stdin:
                        try:
                            result = self.on_tool_call(
                                str(event.raw.get("tool") or ""),
                                event.raw.get("arguments") or {},
                                event.raw,
                            )
                            response = {
                                "type": "tool_response",
                                "request_id": event.raw.get("request_id"),
                                "success": True,
                                "result": result,
                            }
                        except Exception as exc:
                            response = {
                                "type": "tool_response",
                                "request_id": event.raw.get("request_id"),
                                "success": False,
                                "result": {"error": str(exc)},
                            }
                        process.stdin.write(json.dumps(response, ensure_ascii=False) + "\n")
                        process.stdin.flush()
            code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            code = process.wait(timeout=10)
        stderr = ""
        if not events:
            stderr = "app-server produced no JSON events"
        thread_id = thread
        text = ""
        usage = {}
        error = None
        for event in events:
            if event.type == "thread.started":
                thread_id = str(event.raw.get("thread_id") or thread_id or "")
            elif event.type == "turn.completed":
                thread_id = str(event.raw.get("thread_id") or thread_id or "")
                text = str(event.raw.get("text") or "")
                usage = event.raw.get("usage") or {}
            elif event.type in ("bridge.error", "bridge.timeout"):
                error = str(event.raw.get("message") or event.raw.get("error") or event.type)
        if timed_out:
            error = "timeout"
        if not text:
            for event in reversed(events):
                if event.type == "turn.completed":
                    text = str(event.raw.get("text") or "")
                    break
        output_file.write_text(text, encoding="utf-8")
        return CodexRunResult(
            run_id=run_id,
            session_ref=thread_id,
            exit_code=code,
            events=events,
            last_message=text,
            usage=usage,
            stderr=stderr,
            duration_sec=time.monotonic() - started,
            timed_out=timed_out,
            error=error,
        )

    def start(self, prompt: str, *, workspace: Path, output_file: Path, sandbox: str = "workspace-write", model: str | None = None) -> CodexRunResult:
        return self._turn(prompt, workspace, None, output_file)

    def resume(self, session_ref: str, prompt: str, *, workspace: Path, output_file: Path, model: str | None = None) -> CodexRunResult:
        return self._turn(prompt, workspace, session_ref, output_file)
