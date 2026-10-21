"""Honest detection of server capabilities.

The absence of an NPU is a first-class state, not an error: generated data and
CPU reference results may still be useful, but an Ascend C candidate remains
``untested``.  Nothing here fakes a device run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import Settings
from .executor import DockerExecutor, ExecutorError, get_executor

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 20.0


def _run(command: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            text=True,
            errors="replace",
            env={k: v for k, v in os.environ.items() if not k.startswith("ASCENDFORGE_")},
        )
        return completed.returncode, completed.stdout[-4000:], completed.stderr[-2000:]
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def _detect_npu() -> dict:
    tool = shutil.which("npu-smi")
    if not tool:
        return {
            "available": False,
            "status": "unavailable",
            "evidence": "npu-smi not found on PATH",
            "devices": [],
        }
    code, stdout, stderr = _run([tool, "info"])
    devices = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("+", "|", "-", "NPU")):
            devices.append(stripped[:200])
    available = code == 0 and bool(stdout.strip())
    return {
        "available": available,
        "status": "available" if available else "error",
        "evidence": f"npu-smi exit={code}",
        "stderr": stderr[-500:],
        "devices": devices[:8],
    }


def _detect_cann() -> dict:
    candidates = []
    env_home = os.environ.get("ASCEND_HOME_PATH") or os.environ.get("ASCEND_TOOLKIT_HOME")
    if env_home:
        candidates.append(Path(env_home))
    candidates.extend(
        [
            Path("/usr/local/Ascend/ascend-toolkit/latest"),
            Path("/usr/local/Ascend/latest"),
            Path("C:/Program Files/Huawei/Ascend"),
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return {
                "available": True,
                "status": "available",
                "path": str(candidate),
                "evidence": "CANN toolkit directory found",
            }
    return {
        "available": False,
        "status": "unavailable",
        "evidence": "no CANN toolkit directory found",
    }


def _detect_docker(settings: Settings) -> dict:
    docker = DockerExecutor(settings.tmp_dir, settings.executor_image)
    if not shutil.which("docker"):
        return {"available": False, "status": "unavailable", "evidence": "docker not on PATH"}
    available = docker.available()
    image = False
    if available:
        try:
            result = subprocess.run(
                [docker.docker, "image", "inspect", settings.executor_image],
                capture_output=True,
                timeout=5,
            )
            image = result.returncode == 0
        except Exception:
            image = False
    return {
        "available": available,
        "status": "available" if available else "error",
        "image": settings.executor_image,
        "image_present": image,
        "evidence": "docker info succeeded" if available else "docker info failed",
    }


def detect_capabilities(settings: Settings) -> dict:
    key = str(settings.data_dir)
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _TTL:
        return cached[1]
    npu = _detect_npu()
    cann = _detect_cann()
    docker = _detect_docker(settings)
    try:
        executor = get_executor(settings)
        executor_info = {
            "selected": executor.name,
            "network_enforced": bool(getattr(executor, "network_enforced", False)),
        }
        if executor.name == "local":
            executor_info["warning"] = (
                "local subprocess fallback: environment and filesystem are scoped, "
                "but network isolation is not enforced; use Docker on the server"
            )
        if executor.name == "docker" and not docker.get("image_present"):
            executor_info["warning"] = "docker image not present locally; first execution may fail"
    except ExecutorError as exc:
        executor_info = {"selected": None, "error": str(exc)}
    result = {
        "host": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "machine": os.environ.get("PROCESSOR_ARCHITECTURE", "unknown"),
        },
        "npu": npu,
        "cann": cann,
        "docker": docker,
        "executor": executor_info,
        "candidate_execution": {
            "status": "available" if (npu["available"] and cann["available"] and executor_info.get("selected")) else "unavailable",
            "reason": (
                "NPU and CANN toolkit detected"
                if npu["available"] and cann["available"]
                else "no NPU/CANN on this server; Ascend C candidates cannot be executed locally"
            ),
        },
        "cpu_reference": {"status": "available", "note": "Python/C++ reference implementations can run on CPU"},
        "remote_custom_tests": {"status": "unknown", "supported": False},
        "remote_live_verified": {"status": "not_yet", "note": "CANNJudge submit/query await a live account test"},
        "checked_at": time.time(),
        "notes": [
            "No device result is fabricated when no NPU is present.",
            "A CPU reference pass is not an operator pass.",
            "CANNJudge custom test-data upload support is unverified and disabled.",
        ],
    }
    _CACHE[key] = (time.time(), result)
    return result
