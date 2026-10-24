"""CLI Agent capability probing and command construction.

This module does not run an optimization task.  It only reports what the
installed CLI actually supports, without reading or printing credentials.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run(command: list[str], *, cwd: Path | None = None, timeout: float = 30.0) -> dict:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-4000:],
        }
    except FileNotFoundError:
        return {"command": command, "returncode": None, "stdout": "", "stderr": "not found"}
    except subprocess.TimeoutExpired:
        return {"command": command, "returncode": None, "stdout": "", "stderr": "timeout"}


def _which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _features(help_text: str) -> dict[str, bool]:
    text = help_text.lower()
    return {
        "non_interactive_exec": " exec " in f" {text} " or "run codex non-interactively" in text,
        "json_events": "--json" in text,
        "resume_session": " resume " in f" {text} " or "resume a previous" in text,
        "mcp": " mcp " in f" {text} " or "mcp" in text,
        "stdin_prompt": "stdin" in text,
        "output_last_message": "--output-last-message" in text,
        "sandbox_mode": "--sandbox" in text,
    }


def probe_codex() -> dict:
    executable = _which("codex.cmd", "codex.exe", "codex")
    result: dict[str, Any] = {
        "backend": "codex",
        "supported": bool(executable),
        "executable": executable,
        "version": None,
        "authenticated": None,
        "auth_note": None,
        "features": {},
        "notes": [],
    }
    if not executable:
        result["notes"].append("codex executable not found")
        return result
    version = _run([executable, "--version"])
    result["version_result"] = version
    if version["returncode"] == 0:
        result["version"] = version["stdout"].strip().splitlines()[0] if version["stdout"].strip() else None
    login = _run([executable, "login", "status"])
    result["auth_result"] = login
    text = (login["stdout"] + "\n" + login["stderr"]).strip()
    result["auth_note"] = text.splitlines()[0][:200] if text else None
    lowered = text.lower()
    result["authenticated"] = "logged in" in lowered and "not logged" not in lowered
    root_help = _run([executable, "--help"])
    result["root_help"] = root_help
    help_result = _run([executable, "exec", "--help"])
    result["exec_help"] = help_result
    result["features"] = _features(help_result["stdout"] + "\n" + help_result["stderr"])
    result["features"]["mcp"] = result["features"]["mcp"] or " mcp " in f" {(root_help['stdout'] + root_help['stderr']).lower()} "
    resume = _run([executable, "exec", "resume", "--help"])
    result["resume_help"] = resume
    result["features"]["resume_session"] = result["features"]["resume_session"] or resume["returncode"] == 0
    return result


def probe_deepseek() -> dict:
    executable = _which("deepseek.cmd", "deepseek.exe", "deepseek", "deepseek-cli.cmd", "deepseek-cli", "dscli")
    result: dict[str, Any] = {
        "backend": "deepseek",
        "supported": bool(executable),
        "executable": executable,
        "version": None,
        "authenticated": None,
        "auth_note": None,
        "features": {},
        "notes": [],
    }
    if not executable:
        result["notes"].append(
            "no DeepSeek CLI executable found on PATH; install/confirm the real CLI before implementing its adapter"
        )
        return result
    version = _run([executable, "--version"])
    result["version_result"] = version
    if version["returncode"] == 0 and version["stdout"].strip():
        result["version"] = version["stdout"].strip().splitlines()[0]
    help_result = _run([executable, "--help"])
    result["help"] = help_result
    result["features"] = _features(help_result["stdout"] + "\n" + help_result["stderr"])
    return result


def probe_all() -> dict:
    return {
        "codex": probe_codex(),
        "deepseek": probe_deepseek(),
        "note": "Capability probe only. It does not start a model session or submit anything.",
    }


def codex_start_command(
    executable: str,
    *,
    workspace: Path,
    prompt: str,
    output_file: Path,
    sandbox: str = "danger-full-access",
    model: str | None = None,
) -> list[str]:
    command = [
        executable,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "-C",
        str(workspace),
        "-o",
        str(output_file),
    ]
    if model:
        command.extend(["-m", model])
    command.append(prompt)
    return command


def codex_resume_command(
    executable: str,
    *,
    session_ref: str,
    prompt: str,
    output_file: Path | None = None,
    sandbox: str = "danger-full-access",
    model: str | None = None,
) -> list[str]:
    command = [
        executable,
        "exec",
        "resume",
        session_ref,
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if model:
        command.extend(["-m", model])
    if output_file:
        command.extend(["-o", str(output_file)])
    command.append(prompt)
    return command


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(probe_all(), ensure_ascii=False, indent=2))
