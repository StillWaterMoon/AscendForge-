"""Configuration loading for AscendForge.

All configuration comes from environment variables with the ``ASCENDFORGE_``
prefix (optionally via a local ``.env`` file).  State lives under a single
persistent data directory so a container volume or server partition can hold
everything that must survive restarts.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE, # comments, no expansion."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(slots=True)
class Settings:
    root: Path
    data_dir: Path
    db_path: Path
    key_file: Path
    host: str = "127.0.0.1"
    port: int = 8080
    secure_cookies: bool = False
    session_ttl: int = 12 * 3600
    setup_token: str = ""
    worker_id: str = "worker-1"
    worker_poll_seconds: float = 1.0
    lease_seconds: int = 120
    log_level: str = "INFO"
    executor: str = "auto"
    executor_image: str = "python:3.12-slim"
    executor_timeout: int = 30
    executor_memory_mb: int = 512
    executor_cpus: float = 1.0
    executor_allow_network: bool = False
    # Optional fixed-test NPU evaluation backend used by MCP tools.
    # Disabled/empty means "not_configured", never a fabricated result.
    server_eval_enabled: bool = False
    server_eval_command: str = ""
    server_eval_timeout: int = 900
    cannjudge_base_url: str = "https://cannjudge.cn"
    max_package_mb: int = 64
    admin_user: str = "admin"
    admin_password: str = ""
    # Derived at load time, not environment-driven:
    session_secret_hint: str = field(default="", repr=False)

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.secrets_dir,
            self.tasks_dir,
            self.tmp_dir,
            self.logs_dir,
            self.backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Keep generated code / experiment files private by default on POSIX.
        if os.name == "posix":
            for path in (self.data_dir, self.secrets_dir):
                try:
                    path.chmod(0o700)
                except OSError:
                    pass

    def public_dict(self) -> dict:
        """Configuration safe to expose to an authenticated browser."""
        return {
            "product": "AscendForge",
            "product_zh": "昇腾工坊",
            "host": self.host,
            "port": self.port,
            "secure_cookies": self.secure_cookies,
            "worker_id": self.worker_id,
            "executor": self.executor,
            "cannjudge_base_url": self.cannjudge_base_url,
        }

    @classmethod
    def from_env(cls, root: Path | str | None = None) -> "Settings":
        if root is None:
            root = Path(__file__).resolve().parent.parent
        root = Path(root).resolve()
        _load_dotenv(root / ".env")
        data_dir = Path(_env_str("ASCENDFORGE_DATA_DIR", str(root / "data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = (root / data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        setup_token = _env_str("ASCENDFORGE_SETUP_TOKEN")
        settings = cls(
            root=root,
            data_dir=data_dir,
            db_path=Path(_env_str("ASCENDFORGE_DB_PATH", str(data_dir / "ascendforge.db"))),
            key_file=Path(_env_str("ASCENDFORGE_KEY_FILE", str(data_dir / "secrets" / "master.key"))),
            host=_env_str("ASCENDFORGE_HOST", "127.0.0.1"),
            port=_env_int("ASCENDFORGE_PORT", 8080),
            secure_cookies=_env_bool("ASCENDFORGE_SECURE_COOKIES", False),
            session_ttl=max(300, _env_int("ASCENDFORGE_SESSION_TTL", 12 * 3600)),
            setup_token=setup_token,
            worker_id=_env_str("ASCENDFORGE_WORKER_ID", "worker-1") or "worker-1",
            worker_poll_seconds=max(0.2, _env_float("ASCENDFORGE_WORKER_POLL_SECONDS", 1.0)),
            lease_seconds=max(30, _env_int("ASCENDFORGE_LEASE_SECONDS", 120)),
            log_level=_env_str("ASCENDFORGE_LOG_LEVEL", "INFO").upper(),
            executor=_env_str("ASCENDFORGE_EXECUTOR", "auto").lower(),
            executor_image=_env_str("ASCENDFORGE_EXECUTOR_IMAGE", "python:3.12-slim"),
            executor_timeout=max(5, _env_int("ASCENDFORGE_EXECUTOR_TIMEOUT", 30)),
            executor_memory_mb=max(128, _env_int("ASCENDFORGE_EXECUTOR_MEMORY_MB", 512)),
            executor_cpus=max(0.25, _env_float("ASCENDFORGE_EXECUTOR_CPUS", 1.0)),
            executor_allow_network=_env_bool("ASCENDFORGE_EXECUTOR_ALLOW_NETWORK", False),
            server_eval_enabled=_env_bool("ASCENDFORGE_SERVER_EVAL_ENABLED", False),
            server_eval_command=_env_str("ASCENDFORGE_SERVER_EVAL_COMMAND", ""),
            server_eval_timeout=max(30, _env_int("ASCENDFORGE_SERVER_EVAL_TIMEOUT", 900)),
            cannjudge_base_url=_env_str("ASCENDFORGE_CANNJUDGE_BASE_URL", "https://cannjudge.cn").rstrip("/"),
            max_package_mb=max(1, _env_int("ASCENDFORGE_MAX_PACKAGE_MB", 64)),
            admin_user=_env_str("ASCENDFORGE_ADMIN_USER", "admin") or "admin",
            admin_password=_env_str("ASCENDFORGE_ADMIN_PASSWORD"),
        )
        settings.ensure_dirs()
        if not settings.setup_token:
            # A per-install setup token is kept with the encrypted key material.
            # It is printed once by the server if the instance is uninitialised.
            token_file = settings.secrets_dir / "setup.token"
            if token_file.is_file():
                try:
                    settings.setup_token = token_file.read_text(encoding="utf-8").strip()
                except OSError:
                    settings.setup_token = ""
            if not settings.setup_token:
                settings.setup_token = secrets.token_urlsafe(24)
                try:
                    token_file.write_text(settings.setup_token, encoding="utf-8")
                    if os.name == "posix":
                        token_file.chmod(0o600)
                except OSError:
                    pass
        return settings
