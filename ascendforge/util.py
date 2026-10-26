"""Small shared utilities: IDs, hashing, time, safe paths and JSON."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

TZ_NAME = "Asia/Shanghai"


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    """Short, collision-resistant, human-readable identifier."""
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_dumps(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))




def hmac_digest(key: bytes, data: bytes) -> str:
    import hmac

    return hmac.new(key, data, hashlib.sha256).hexdigest()


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None and os.name == "posix":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    atomic_write(path, text.encode("utf-8"), mode=mode)


def truncate_text(text: str, limit: int, marker: str = "\n...[truncated]...") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def safe_relpath(rel: str) -> str:
    """Validate a POSIX-style relative path received from a model or ZIP.

    Rejects absolute paths, drive letters, backslashes, NUL bytes, ``..`` and
    empty segments.  Returns the normalised path.
    """
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        raise ValueError("invalid relative path")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ValueError("absolute paths are not allowed")
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise ValueError("absolute paths are not allowed")
    parts = p.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("path traversal is not allowed")
    if len(rel) > 255:
        raise ValueError("path too long")
    return str(p)












def shanghai_day(ts: float | None = None) -> str:
    """Calendar day in the configured quota timezone (default Asia/Shanghai)."""
    ts = now() if ts is None else ts
    try:
        from zoneinfo import ZoneInfo

        moment = datetime.fromtimestamp(ts, tz=ZoneInfo(TZ_NAME))
    except Exception:  # pragma: no cover - tzdata missing
        # Asia/Shanghai has been UTC+8 without DST since 1991.
        from datetime import timedelta

        moment = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    return moment.strftime("%Y-%m-%d")


def next_day_start(ts: float | None = None) -> float:
    ts = now() if ts is None else ts
    try:
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(TZ_NAME)
    except Exception:  # pragma: no cover - tzdata missing
        from datetime import timedelta

        tz = timezone(timedelta(hours=8))
    try:
        moment = datetime.fromtimestamp(ts, tz=tz)
        tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.timestamp()
    except Exception:  # pragma: no cover
        return ts + 86400


def stable_hash_files(files: dict[str, str]) -> tuple[str, list[dict]]:
    """Hash a logical file map deterministically.

    Returns (aggregate_sha256, manifest).  Hashing is over canonical
    ``path\\0sha256\\0size\\n`` lines so it is reproducible across hosts.
    """
    manifest: list[dict] = []
    lines: list[str] = []
    for path in sorted(files):
        content = files[path]
        if not isinstance(content, str):
            raise ValueError(f"file content must be text: {path}")
        encoded = content.encode("utf-8")
        digest = sha256_bytes(encoded)
        manifest.append({"path": path, "sha256": digest, "bytes": len(encoded)})
        lines.append(f"{path}\x00{digest}\x00{len(encoded)}")
    aggregate = sha256_text("\n".join(lines))
    return aggregate, manifest


