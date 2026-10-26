"""Local Codex CLI adapter.

The adapter deliberately uses a task workspace instead of injecting the full
platform prompt.  The host prepares problem/template/history files in the
workspace; Codex edits only that workspace; the host then collects the changed
files as a candidate.  Official submission remains host-side.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .probe import codex_resume_command, codex_start_command, probe_codex


@dataclass
class CliEvent:
    type: str
    raw: dict


@dataclass
class CodexRunResult:
    run_id: str
    session_ref: str | None
    exit_code: int | None
    events: list[CliEvent] = field(default_factory=list)
    last_message: str = ""
    usage: dict = field(default_factory=dict)
    stderr: str = ""
    duration_sec: float = 0.0
    timed_out: bool = False
    error: str | None = None
    error_code: str | None = None
    error_stage: str | None = None
    error_thread_id: str | None = None
    error_request_id: str | None = None
    diagnostics: list[dict] = field(default_factory=list)


def parse_jsonl(text: str) -> list[CliEvent]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type"):
            events.append(CliEvent(type=str(value["type"]), raw=value))
    return events


def _collect_usage(events: list[CliEvent]) -> dict:
    for event in reversed(events):
        if event.type == "turn.completed" and isinstance(event.raw.get("usage"), dict):
            return event.raw["usage"]
    return {}


def _collect_session(events: list[CliEvent]) -> str | None:
    for event in events:
        if event.type == "thread.started" and event.raw.get("thread_id"):
            return str(event.raw["thread_id"])
    return None


class CodexAdapter:
    def __init__(self, executable: str | None = None, *, timeout_sec: float = 1800):
        self.executable = executable or (probe_codex().get("executable") or "codex.cmd")
        self.timeout_sec = timeout_sec

    def probe(self) -> dict:
        return probe_codex()

    def prepare_workspace(
        self,
        directory: Path,
        files: dict[str, str],
        *,
        task_meta: dict | None = None,
        instructions: str = "",
    ) -> Path:
        workspace = Path(directory) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (workspace / "TASK.json").write_text(
            json.dumps(task_meta or {}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (workspace / "AGENTS.md").write_text(
            "# Task workspace\n\n"
            "Only modify source files under this workspace. Do not access credentials, "
            "the platform database, or submit to the official judge.\n\n"
            + instructions,
            encoding="utf-8",
        )
        return workspace

    def collect_changes(self, workspace: Path, baseline: dict[str, str]) -> dict[str, str]:
        changes = {}
        for rel, old in baseline.items():
            path = Path(workspace) / rel
            if not path.exists():
                continue
            new = path.read_text(encoding="utf-8")
            if new != old:
                changes[rel] = new
        return changes

    def _execute(self, command: list[str], *, cwd: Path, output_file: Path, started: float) -> CodexRunResult:
        run_id = f"codex-{int(time.time() * 1000)}"
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_sec,
                shell=False,
            )
            stdout, stderr = completed.stdout, completed.stderr
            exit_code = completed.returncode
            timed_out = False
            error = None
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            exit_code = None
            timed_out = True
            error = "timeout"
        except OSError as exc:
            return CodexRunResult(
                run_id=run_id,
                session_ref=None,
                exit_code=None,
                stderr=str(exc),
                duration_sec=time.monotonic() - started,
                error=f"spawn failed: {exc}",
            )
        events = parse_jsonl(stdout)
        last = ""
        if output_file.exists():
            last = output_file.read_text(encoding="utf-8", errors="replace")
        else:
            for event in reversed(events):
                if event.type == "item.completed":
                    item = event.raw.get("item") or {}
                    if item.get("type") == "agent_message":
                        last = str(item.get("text") or "")
                        break
        return CodexRunResult(
            run_id=run_id,
            session_ref=_collect_session(events),
            exit_code=exit_code,
            events=events,
            last_message=last,
            usage=_collect_usage(events),
            stderr=stderr,
            duration_sec=time.monotonic() - started,
            timed_out=timed_out,
            error=error,
        )

    def start(
        self,
        prompt: str,
        *,
        workspace: Path,
        output_file: Path,
        sandbox: str = "workspace-write",
        model: str | None = None,
    ) -> CodexRunResult:
        started = time.monotonic()
        command = codex_start_command(
            self.executable,
            workspace=workspace,
            prompt=prompt,
            output_file=output_file,
            sandbox=sandbox,
            model=model,
        )
        return self._execute(command, cwd=workspace, output_file=output_file, started=started)

    def resume(
        self,
        session_ref: str,
        prompt: str,
        *,
        workspace: Path,
        output_file: Path,
        model: str | None = None,
    ) -> CodexRunResult:
        started = time.monotonic()
        command = codex_resume_command(
            self.executable,
            session_ref=session_ref,
            prompt=prompt,
            output_file=output_file,
            model=model,
        )
        return self._execute(command, cwd=workspace, output_file=output_file, started=started)
