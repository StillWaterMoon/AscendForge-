"""Authentication, credential encryption and secret handling.

Rules enforced here:
* Passwords are never stored in plaintext (scrypt with a per-user salt).
* Model API keys and CANNJudge cookies are encrypted at rest with a Fernet key
  stored outside the database (``secrets/master.key`` or env).
* Secrets are never returned to the browser; only masked summaries are.
* Session cookies are random opaque tokens; only their SHA-256 is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .db import Database
from .util import hmac_digest, new_id, now, redact

try:  # cryptography is the only runtime dependency
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - deployment error path
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]


class SecurityError(RuntimeError):
    """Raised for configuration/security failures; message is safe to log."""


def load_master_key(key_file: Path, env_key: str = "") -> bytes:
    if Fernet is None:  # pragma: no cover
        raise SecurityError(
            "cryptography is required for credential encryption; "
            "install dependencies with: python -m pip install -r requirements.txt"
        )
    key = (env_key or "").strip().encode("utf-8")
    if key:
        try:
            Fernet(key)
        except Exception as exc:
            raise SecurityError("ASCENDFORGE_SECRET_KEY is not a valid Fernet key") from exc
        return key
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.is_file():
        key = key_file.read_bytes().strip()
        try:
            Fernet(key)
            return key
        except Exception as exc:
            raise SecurityError(f"invalid master key file: {key_file}") from exc
    key = Fernet.generate_key()
    try:
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Web and worker may start simultaneously on a fresh data directory.
        # The other process may still be writing; retry briefly.
        for _ in range(40):
            try:
                existing = key_file.read_bytes().strip()
                Fernet(existing)
                return existing
            except Exception:
                time.sleep(0.05)
        raise SecurityError(f"invalid master key file: {key_file}")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if os.name == "posix":
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
    return key


class SecretBox:
    def __init__(self, key: bytes):
        if Fernet is None:  # pragma: no cover
            raise SecurityError("cryptography is not installed")
        self._fernet = Fernet(key)
        self._key = key

    def encrypt(self, payload: dict | str) -> str:
        raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def decrypt(self, token: str) -> Any:
        try:
            raw = self._fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise SecurityError("stored credential cannot be decrypted with the current key") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    def fingerprint(self, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hmac_digest(self._key, raw)[:16]


class CredentialStore:
    """Encrypted key/value store for model config and platform sessions."""

    def __init__(self, db: Database, box: SecretBox):
        self.db = db
        self.box = box

    def put(self, kind: str, payload: dict) -> None:
        ciphertext = self.box.encrypt(payload)
        fingerprint = self.box.fingerprint(payload)
        self.db.execute(
            "INSERT INTO credentials(kind, ciphertext, fingerprint, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET ciphertext=excluded.ciphertext, "
            "fingerprint=excluded.fingerprint, updated_at=excluded.updated_at",
            (kind, ciphertext, fingerprint, now()),
        )

    def get(self, kind: str) -> dict | None:
        row = self.db.query_one("SELECT ciphertext FROM credentials WHERE kind=?", (kind,))
        if not row:
            return None
        value = self.box.decrypt(row["ciphertext"])
        return value if isinstance(value, dict) else None

    def has(self, kind: str) -> bool:
        return bool(self.db.scalar("SELECT 1 FROM credentials WHERE kind=?", (kind,)))

    def delete(self, kind: str) -> None:
        self.db.execute("DELETE FROM credentials WHERE kind=?", (kind,))


def mask_key(value: str | None) -> str:
    if not value:
        return ""
    return redact(value, keep=4)


def model_settings_public(payload: dict | None) -> dict:
    if not payload:
        return {"configured": False}
    return {
        "configured": True,
        "base_url": payload.get("base_url", ""),
        "model": payload.get("model", ""),
        "provider": payload.get("provider", "openai-compatible"),
        "api_key_masked": mask_key(payload.get("api_key")),
        "supports_tool_calls": bool(payload.get("supports_tool_calls", False)),
        "max_tokens": payload.get("max_tokens", 2048),
        "temperature": payload.get("temperature", 0.2),
        "timeout": payload.get("timeout", 120),
        "reasoning_mode": payload.get("reasoning_mode", "auto"),
        "price_prompt_per_1k": payload.get("price_prompt_per_1k"),
        "price_completion_per_1k": payload.get("price_completion_per_1k"),
        "updated_at": payload.get("updated_at"),
    }


def platform_session_public(payload: dict | None) -> dict:
    if not payload:
        return {"connected": False}
    return {
        "connected": bool(payload.get("user_id") and payload.get("cookies")),
        "base_url": payload.get("base_url"),
        "user_id": payload.get("user_id"),
        "account": payload.get("account"),
        "logged_in_at": payload.get("logged_in_at"),
        "verified_at": payload.get("verified_at"),
        "last_error": payload.get("last_error"),
    }


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, hash_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(
    db: Database,
    user_id: str,
    ttl_seconds: int,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    current = now()
    db.execute(
        "INSERT INTO sessions(id, user_id, token_hash, csrf_token, created_at, last_seen_at, "
        "expires_at, ip, user_agent) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("sess"),
            user_id,
            _token_hash(raw),
            csrf,
            current,
            current,
            current + ttl_seconds,
            (ip or "")[:64],
            (user_agent or "")[:256],
        ),
    )
    return raw, csrf


def lookup_session(db: Database, raw_token: str | None) -> dict | None:
    if not raw_token:
        return None
    row = db.query_one(
        "SELECT s.*, u.username, u.disabled FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at>?",
        (_token_hash(raw_token), now()),
    )
    if not row or row.get("disabled"):
        return None
    db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now(), row["id"]))
    return row


def revoke_session(db: Database, session_id: str) -> None:
    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def purge_expired_sessions(db: Database) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
    db.execute("DELETE FROM idempotency WHERE expires_at<?", (now(),))


def csrf_valid(session: dict, supplied: str | None) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(str(session.get("csrf_token", "")), supplied)


def setup_token_valid(expected: str, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected, supplied)


# ---------------------------------------------------------------------------
# Login throttling (per process; sufficient for single-user deployment)
# ---------------------------------------------------------------------------


class LoginThrottle:
    def __init__(self, limit: int = 10, window: int = 300):
        self.limit = limit
        self.window = window
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        current = time.monotonic()
        with self._lock:
            events = [t for t in self._events.get(key, []) if current - t < self.window]
            self._events[key] = events
            return len(events) < self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._events.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


def app_version() -> str:
    return __version__
