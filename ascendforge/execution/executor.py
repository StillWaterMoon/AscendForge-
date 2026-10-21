"""Resource-limited execution backends.

Two backends exist:

* ``docker`` — strongly recommended on the server.  No network, memory/CPU/PID
  limits and a disposable filesystem.
* ``local`` — subprocess fallback for workstations without Docker.  POSIX
  resource limits are applied, the environment is scrubbed and the working
  directory is a throw-away directory, but network isolation is *not* enforced.
  This limitation is reported explicitly rather than hidden.

Generated code must never see credentials, the database, sessions or the
encryption key: the workspace is created under ``DATA_DIR/tmp`` with only the
files explicitly passed in, and only a minimal environment is forwarded.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..util import new_id, safe_relpath

MAX_WORKSPACE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_FILES = 200


class ExecutorError(RuntimeError):
    pass


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    backend: str
    network_enforced: bool
    truncated: bool = False
    artifacts: dict[str, bytes] = field(default_factory=dict)
    error: str | None = None
    limits: dict = field(default_factory=dict)

    def public(self, *, include_artifacts: bool = False) -> dict:
        result = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "backend": self.backend,
            "network_enforced": self.network_enforced,
            "truncated": self.truncated,
            "error": self.error,
            "limits": self.limits,
        }
        if include_artifacts:
            result["artifacts"] = {
                path: {"bytes": len(data), "sha256": __import__("hashlib").sha256(data).hexdigest()}
                for path, data in self.artifacts.items()
            }
        return result


def _read_limited(stream: Any, sink: list[bytes], limit: int, state: dict) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if state["bytes"] < limit:
                remaining = limit - state["bytes"]
                sink.append(chunk[:remaining])
                state["bytes"] += min(len(chunk), remaining)
            if state["bytes"] >= limit:
                state["truncated"] = True
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _minimal_env(workspace: Path) -> dict[str, str]:
    keep = (
        "PATH",
        "SystemRoot",
        "TEMP",
        "TMP",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "TZ",
    )
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(
        {
            "HOME": str(workspace),
            "TMPDIR": str(workspace),
            "TEMP": str(workspace),
            "TMP": str(workspace),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _write_workspace(root: Path, files: dict[str, str]) -> None:
    total = 0
    for raw_path, content in files.items():
        path = safe_relpath(raw_path)
        if not isinstance(content, str):
            raise ExecutorError(f"execution input must be text: {path}")
        data = content.encode("utf-8")
        total += len(data)
        if total > MAX_WORKSPACE_BYTES:
            raise ExecutorError("execution workspace too large")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _collect_artifacts(root: Path) -> dict[str, bytes]:
    out = root / "out"
    if not out.is_dir():
        return {}
    artifacts: dict[str, bytes] = {}
    total = 0
    for index, path in enumerate(sorted(out.rglob("*"))):
        if index >= MAX_ARTIFACT_FILES:
            break
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_ARTIFACT_BYTES:
            break
        rel = path.relative_to(root).as_posix()
        artifacts[rel] = path.read_bytes()
    return artifacts


class BaseExecutor:
    name = "base"
    network_enforced = False

    def run(
        self,
        files: dict[str, str],
        command: list[str],
        *,
        timeout: int,
        memory_mb: int = 512,
        cpus: float = 1.0,
        allow_network: bool = False,
    ) -> ExecutionResult:
        raise NotImplementedError


class LocalExecutor(BaseExecutor):
    name = "local"

    def __init__(self, tmp_root: Path):
        self.tmp_root = tmp_root

    def run(
        self,
        files: dict[str, str],
        command: list[str],
        *,
        timeout: int,
        memory_mb: int = 512,
        cpus: float = 1.0,
        allow_network: bool = False,
    ) -> ExecutionResult:
        if not command or not all(isinstance(part, str) for part in command):
            raise ExecutorError("command must be a non-empty list of strings")
        if len(command) > 32:
            raise ExecutorError("command has too many arguments")
        workspace = Path(tempfile.mkdtemp(prefix=f"exec-{new_id('run')}-", dir=str(self.tmp_root)))
        try:
            _write_workspace(workspace, files)
            env = _minimal_env(workspace)
            limits: dict[str, Any] = {
                "timeout_sec": timeout,
                "memory_mb": memory_mb,
                "cpus": cpus,
                "max_output_bytes": MAX_OUTPUT_BYTES,
                "network_enforced": False,
            }
            popen_kwargs: dict[str, Any] = {
                "cwd": str(workspace),
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "stdin": subprocess.DEVNULL,
                "start_new_session": os.name == "posix",
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kwargs["preexec_fn"] = _posix_limits(timeout, memory_mb, cpus)
            started = time.monotonic()
            proc = subprocess.Popen(command, **popen_kwargs)
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            stdout_state = {"bytes": 0, "truncated": False}
            stderr_state = {"bytes": 0, "truncated": False}
            threads = [
                threading.Thread(target=_read_limited, args=(proc.stdout, stdout_chunks, MAX_OUTPUT_BYTES, stdout_state), daemon=True),
                threading.Thread(target=_read_limited, args=(proc.stderr, stderr_chunks, MAX_OUTPUT_BYTES, stderr_state), daemon=True),
            ]
            for thread in threads:
                thread.start()
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            for thread in threads:
                thread.join(timeout=2)
            duration = int((time.monotonic() - started) * 1000)
            stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            artifacts = _collect_artifacts(workspace)
            return ExecutionResult(
                ok=(not timed_out and proc.returncode == 0),
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                duration_ms=duration,
                backend=self.name,
                network_enforced=False,
                truncated=bool(stdout_state["truncated"] or stderr_state["truncated"]),
                artifacts=artifacts,
                error="timed out" if timed_out else None,
                limits=limits,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


def _posix_limits(timeout: int, memory_mb: int, cpus: float):
    def apply() -> None:
        try:
            import resource

            cpu = int(max(1, timeout))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
            resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
            except (ValueError, OSError):
                pass
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            except (ValueError, OSError):
                pass
            os.setsid()
        except Exception:
            # Limits are best-effort in constrained environments.
            pass

    return apply


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class DockerExecutor(BaseExecutor):
    name = "docker"
    network_enforced = True

    def __init__(self, tmp_root: Path, image: str, *, allow_network: bool = False):
        self.tmp_root = tmp_root
        self.image = image
        self.allow_network = allow_network
        self.docker = shutil.which("docker")

    def available(self) -> bool:
        if not self.docker:
            return False
        try:
            result = subprocess.run(
                [self.docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                timeout=5,
                text=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def run(
        self,
        files: dict[str, str],
        command: list[str],
        *,
        timeout: int,
        memory_mb: int = 512,
        cpus: float = 1.0,
        allow_network: bool = False,
    ) -> ExecutionResult:
        if not self.docker:
            raise ExecutorError("docker is not installed")
        workspace = Path(tempfile.mkdtemp(prefix=f"exec-{new_id('run')}-", dir=str(self.tmp_root)))
        name = f"ascendforge-exec-{new_id('c')}"
        try:
            _write_workspace(workspace, files)
            network = "bridge" if (allow_network or self.allow_network) else "none"
            args = [
                self.docker,
                "run",
                "--rm",
                "--name",
                name,
                "--network",
                network,
                "--memory",
                f"{int(memory_mb)}m",
                "--cpus",
                str(cpus),
                "--pids-limit",
                "128",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "-v",
                f"{workspace}:/work:rw",
                "-w",
                "/work",
            ]
            if os.name == "posix" and hasattr(os, "getuid"):
                args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
            args.extend([self.image, *command])
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    args,
                    capture_output=True,
                    timeout=timeout + 5,
                    text=False,
                )
                timed_out = False
                stdout = completed.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                stderr = completed.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                exit_code = completed.returncode
                truncated = len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                subprocess.run([self.docker, "rm", "-f", name], capture_output=True, timeout=10)
                stdout = (exc.stdout or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                stderr = (exc.stderr or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                exit_code = None
                truncated = False
            duration = int((time.monotonic() - started) * 1000)
            artifacts = _collect_artifacts(workspace)
            return ExecutionResult(
                ok=(not timed_out and exit_code == 0),
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                duration_ms=duration,
                backend=self.name,
                network_enforced=(network == "none"),
                truncated=truncated,
                artifacts=artifacts,
                error="timed out" if timed_out else None,
                limits={
                    "timeout_sec": timeout,
                    "memory_mb": memory_mb,
                    "cpus": cpus,
                    "network": network,
                    "image": self.image,
                    "max_output_bytes": MAX_OUTPUT_BYTES,
                },
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


_IMAGE_CACHE: dict[tuple[str, str], bool] = {}


def _docker_usable(settings: Settings, executor: DockerExecutor) -> bool:
    key = (str(settings.tmp_dir), executor.image)
    if key not in _IMAGE_CACHE:
        if not executor.available():
            _IMAGE_CACHE[key] = False
        else:
            try:
                result = subprocess.run(
                    [executor.docker, "image", "inspect", executor.image],
                    capture_output=True,
                    timeout=5,
                )
                _IMAGE_CACHE[key] = result.returncode == 0
            except Exception:
                _IMAGE_CACHE[key] = False
    return _IMAGE_CACHE[key]


def get_executor(settings: Settings) -> BaseExecutor:
    choice = (settings.executor or "auto").lower()
    docker = DockerExecutor(settings.tmp_dir, settings.executor_image, allow_network=settings.executor_allow_network)
    if choice == "docker":
        if not _docker_usable(settings, docker):
            raise ExecutorError(
                "executor=docker but Docker or the configured image is unavailable; "
                "set ASCENDFORGE_EXECUTOR=local only if you accept weaker isolation"
            )
        return docker
    if choice == "local":
        return LocalExecutor(settings.tmp_dir)
    if _docker_usable(settings, docker):
        return docker
    return LocalExecutor(settings.tmp_dir)
