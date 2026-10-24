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

    def __init__(self, executable: str | None = None, *, timeout_sec: float = 1800):
        probe = probe_codex()
        self.executable = executable or probe.get("executable") or "codex.cmd"
        self.timeout_sec = timeout_sec
        self.node = shutil.which("node") or "node"
        self.port = _free_port()
        self.server = subprocess.Popen(
            [self.executable, "app-server", "--listen", f"ws://127.0.0.1:{self.port}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # The server binds quickly; a short retry is cheaper than guessing.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.15)
        self.close()
        raise RuntimeError("Codex app-server did not start")

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
        command = [
            self.node,
            str(script),
            f"port={self.port}",
            f"cwd={workspace}",
            f"prompt={prompt}",
            f"thread={thread or ''}",
            f"timeout_ms={int(self.timeout_sec * 1000)}",
        ]
        started = time.monotonic()
        run_id = f"appserver-{int(time.time() * 1000)}"
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_sec + 30,
                shell=False,
            )
            stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, code = exc.stdout or "", exc.stderr or "", None
            timed_out = True
        events = parse_jsonl(stdout)
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
