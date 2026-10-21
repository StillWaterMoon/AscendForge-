"""Browser control surface: stdlib HTTP server + JSON API + static UI.

Deliberately dependency-free so the only server prerequisite is Python 3.11+ and
``cryptography``.  The browser only polls JSON; it never owns the optimization
loop.  Mutating requests support an ``X-Request-Id`` idempotency key and require
a CSRF token tied to the server-side session.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .agent.protocol import TOOLS
from .agent.providers import ModelConfig, ProviderError, make_provider
from .agent.skills import SkillError, SkillRegistry
from .backup import BackupError, create_backup, list_backups, verify_backup
from .baseline import create_baseline_submission
from .config import Settings
from .db import Database
from .execution.capabilities import detect_capabilities
from .experiments import (
    SnapshotError,
    add_event,
    archive_integrity,
    attempt_history,
    build_snapshot_zip,
    build_task_archive,
    get_attempt,
    read_snapshot,
    unified_source_diff,
)
from .logging_setup import configure_logging
from .platform.base import PlatformError
from .platform.service import DEFAULT_TEST_PROBLEM, PlatformService
from .scheduler import Scheduler
from .security import (
    CredentialStore,
    LoginThrottle,
    SecretBox,
    create_session,
    csrf_valid,
    hash_password,
    load_master_key,
    lookup_session,
    mask_key,
    model_settings_public,
    purge_expired_sessions,
    revoke_session,
    setup_token_valid,
    verify_password,
)
from .states import TASK_TERMINAL
from .tasks import TaskError, TaskService
from .util import canonical_json, iso, json_dumps, json_loads, new_id, now, safe_relpath, sha256_bytes, truncate_text

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024
COOKIE_NAME = "af_session"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def public(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


@dataclass(slots=True)
class FilePayload:
    path: Path
    filename: str
    content_type: str = "application/octet-stream"


@dataclass(slots=True)
class Route:
    method: str
    pattern: re.Pattern
    handler: str
    public: bool = False
    csrf_exempt: bool = False


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.init()
        self.box = SecretBox(load_master_key(settings.key_file, os.environ.get("ASCENDFORGE_SECRET_KEY", "")))
        self.credentials = CredentialStore(self.db, self.box)
        self.platform = PlatformService(self.db, settings, self.credentials)
        self.tasks = TaskService(self.db, settings, self.platform)
        self.scheduler = Scheduler(self.db, settings, self.platform, self.tasks)
        self.skills = SkillRegistry(settings.root / "skills")
        self.throttle = LoginThrottle()
        self.idem_lock = threading.Lock()
        self.setup_token = settings.setup_token
        self.routes = self._routes()
        self._bootstrap_admin()
        purge_expired_sessions(self.db)
        if self.needs_setup():
            # Printed to the console (not the rotating log) so an operator can
            # complete first-run setup.  It is not a user credential.
            print(
                "\nAscendForge first-run setup required.\n"
                f"Open the web UI and enter this setup token: {self.setup_token}\n",
                file=sys.stderr,
                flush=True,
            )

    # ------------------------------------------------------------------
    def _bootstrap_admin(self) -> None:
        count = int(self.db.scalar("SELECT COUNT(*) FROM users") or 0)
        if count or not self.settings.admin_password:
            return
        try:
            password_hash = hash_password(self.settings.admin_password)
        except ValueError as exc:
            log.error("ASCENDFORGE_ADMIN_PASSWORD rejected: %s", exc)
            return
        self.db.execute(
            "INSERT INTO users(id, username, password_hash, created_at) VALUES(?, ?, ?, ?)",
            (new_id("user"), self.settings.admin_user, password_hash, now()),
        )
        log.info("created initial admin user %s from environment", self.settings.admin_user)

    def needs_setup(self) -> bool:
        return int(self.db.scalar("SELECT COUNT(*) FROM users") or 0) == 0

    def worker_state(self) -> dict:
        heartbeat = self.db.setting_get("worker_heartbeat", None)
        if not heartbeat:
            return {"alive": False, "state": "unknown", "heartbeat": None}
        age = now() - float(heartbeat.get("at") or 0)
        return {
            "alive": age < 30,
            "age_sec": round(age, 1),
            "state": heartbeat.get("state"),
            "worker_id": heartbeat.get("worker_id"),
            "at": heartbeat.get("at"),
        }

    def platform_public(self) -> dict:
        return self.platform.status()

    def capabilities(self) -> dict:
        return detect_capabilities(self.settings)

    def model_public(self) -> dict:
        payload = self.credentials.get("model")
        data = model_settings_public(payload)
        if payload:
            data["endpoint"] = ModelConfig.from_payload(payload).endpoint()
        return data

    def summary(self) -> dict:
        counts = self.tasks.status_counts()
        recent = self.tasks.list()[:8]
        model = self.model_public()
        platform = self.platform_public()
        capabilities = detect_capabilities(self.settings)
        return {
            "product": "AscendForge",
            "product_zh": "昇腾工坊",
            "version": __version__,
            "counts": counts,
            "tasks": recent,
            "worker": self.worker_state(),
            "model": {"configured": bool(model.get("configured")), "model": model.get("model")},
            "platform": {
                "connected": platform.get("connected"),
                "judge_verified": platform.get("judge_verified"),
                "account_masked": platform.get("account_masked"),
            },
            "capabilities": {
                "npu": capabilities.get("npu", {}).get("status"),
                "candidate_execution": capabilities.get("candidate_execution", {}).get("status"),
                "executor": capabilities.get("executor", {}).get("selected"),
                "remote_custom_tests": capabilities.get("remote_custom_tests", {}).get("supported"),
            },
            "skills": self.skills.list(),
            "next_submit_hint": "见任务详情中的调度额度",
        }

    # ------------------------------------------------------------------
    def _routes(self) -> list[Route]:
        raw = [
            ("GET", r"^/api/health$", "h_health", True, True),
            ("GET", r"^/api/setup/status$", "h_setup_status", True, True),
            ("POST", r"^/api/setup$", "h_setup", True, True),
            ("POST", r"^/api/auth/login$", "h_login", True, True),
            ("GET", r"^/api/auth/me$", "h_me", True, True),
            ("POST", r"^/api/auth/logout$", "h_logout", False, False),
            ("POST", r"^/api/auth/password$", "h_password", False, False),
            ("GET", r"^/api/summary$", "h_summary", False, True),
            ("GET", r"^/api/tasks$", "h_tasks_list", False, True),
            ("POST", r"^/api/tasks$", "h_tasks_create", False, False),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)$", "h_task_detail", False, True),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/(?P<action>start|pause|resume|stop)$", "h_task_control", False, False),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/baseline-submit$", "h_baseline_submit", False, False),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/continue-optimization$", "h_continue_optimization", False, False),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/events$", "h_task_events", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/attempts$", "h_task_attempts", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/attempts/(?P<attempt_id>[A-Za-z0-9_-]+)$", "h_attempt_detail", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/attempts/(?P<attempt_id>[A-Za-z0-9_-]+)/download$", "h_attempt_download", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/download/best$", "h_best_download", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/archive$", "h_task_archive", False, True),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/submissions/(?P<submission_id>[A-Za-z0-9_-]+)/resolve$", "h_resolve_submission", False, False),
            ("GET", r"^/api/settings/model$", "h_model_get", False, True),
            ("PUT", r"^/api/settings/model$", "h_model_put", False, False),
            ("DELETE", r"^/api/settings/model$", "h_model_delete", False, False),
            ("POST", r"^/api/settings/model/test$", "h_model_test", False, False),
            ("GET", r"^/api/platform/status$", "h_platform_status", False, True),
            ("GET", r"^/api/platform/captcha$", "h_platform_captcha", False, True),
            ("POST", r"^/api/platform/login$", "h_platform_login", False, False),
            ("POST", r"^/api/platform/import-session$", "h_platform_import", False, False),
            ("POST", r"^/api/platform/test$", "h_platform_test", False, False),
            ("POST", r"^/api/platform/disconnect$", "h_platform_disconnect", False, False),
            ("GET", r"^/api/capabilities$", "h_capabilities", False, True),
            ("GET", r"^/api/skills$", "h_skills", False, True),
            ("GET", r"^/api/worker$", "h_worker", False, True),
            ("GET", r"^/api/backups$", "h_backups_list", False, True),
            ("POST", r"^/api/backups$", "h_backups_create", False, False),
            ("GET", r"^/api/backups/(?P<name>[A-Za-z0-9_.-]+)$", "h_backup_download", False, True),
        ]
        return [Route(method, re.compile(pattern), handler, public, csrf) for method, pattern, handler, public, csrf in raw]


# ===========================================================================
# HTTP handler
# ===========================================================================


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"AscendForge/{__version__}"
    sys_version = ""

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _session(self) -> dict | None:
        if getattr(self, "_session_loaded", False):
            return self._session_cache
        raw = self._cookie(COOKIE_NAME)
        session = lookup_session(self.app.db, raw)
        self._session_cache = session
        self._session_loaded = True
        return session

    def _cookie(self, name: str) -> str | None:
        header = self.headers.get("Cookie", "")
        if not header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            return None
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def _set_session_cookie(self, raw: str) -> None:
        parts = [
            f"{COOKIE_NAME}={raw}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={self.app.settings.session_ttl}",
        ]
        if self.app.settings.secure_cookies:
            parts.append("Secure")
        self._extra_cookie = "; ".join(parts)

    def _clear_session_cookie(self) -> None:
        parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
        if self.app.settings.secure_cookies:
            parts.append("Secure")
        self._extra_cookie = "; ".join(parts)

    def _client_ip(self) -> str:
        return str(self.client_address[0]) if self.client_address else "unknown"

    def _read_json(self) -> dict:
        length = self.headers.get("Content-Length")
        if length is None:
            return {}
        try:
            size = int(length)
        except ValueError:
            raise ApiError(400, "BAD_BODY", "invalid Content-Length") from None
        if size <= 0:
            return {}
        if size > MAX_BODY_BYTES:
            raise ApiError(413, "BODY_TOO_LARGE", "request body too large")
        raw = self.rfile.read(size)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "BAD_JSON", "request body must be valid JSON") from None
        if not isinstance(value, dict):
            raise ApiError(400, "BAD_JSON", "request body must be a JSON object")
        return value

    def _send_bytes(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if getattr(self, "_extra_cookie", None):
            self.send_header("Set-Cookie", self._extra_cookie)
            self._extra_cookie = None
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json_dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_file(self, payload: FilePayload) -> None:
        path = payload.path
        if not path.is_file():
            self._send_json(404, ApiError(404, "FILE_NOT_FOUND", "file not found").public())
            return
        try:
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", payload.content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{payload.filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if getattr(self, "_extra_cookie", None):
                self.send_header("Set-Cookie", self._extra_cookie)
                self._extra_cookie = None
            self.end_headers()
            with open(path, "rb") as handle:
                shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- dispatch ------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        self.query = urllib.parse.parse_qs(parsed.query)
        self._extra_cookie = None
        self._session_cache = None
        self._session_loaded = False
        try:
            if method == "GET" and path in ("/", "/index.html"):
                self._serve_static("index.html", "text/html; charset=utf-8")
                return
            if method == "GET" and path.startswith("/static/"):
                name = path[len("/static/") :]
                if name not in ("app.js", "style.css"):
                    raise ApiError(404, "NOT_FOUND", "not found")
                content_type = "application/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
                self._serve_static(name, content_type)
                return
            if method == "GET" and path == "/favicon.ico":
                self._send_bytes(204, b"", "image/x-icon")
                return
            route = next((r for r in self.app.routes if r.method == method and r.pattern.match(path)), None)
            if not route:
                raise ApiError(404, "NOT_FOUND", "endpoint not found")
            match = route.pattern.match(path)
            assert match is not None
            session = self._session()
            if not route.public and not session:
                raise ApiError(401, "AUTH_REQUIRED", "请先登录 AscendForge")
            if method in ("POST", "PUT", "DELETE") and not route.csrf_exempt:
                if not session:
                    raise ApiError(401, "AUTH_REQUIRED", "请先登录 AscendForge")
                if not csrf_valid(session, self.headers.get("X-CSRF-Token")):
                    raise ApiError(403, "CSRF", "CSRF token missing or invalid; refresh the page")
                origin = self.headers.get("Origin")
                if origin:
                    host = self.headers.get("Host", "")
                    if urllib.parse.urlsplit(origin).netloc != host:
                        raise ApiError(403, "ORIGIN", "cross-origin request rejected")
            idem_key = self.headers.get("X-Request-Id") if method in ("POST", "PUT", "DELETE") else None
            if idem_key and not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", idem_key):
                raise ApiError(400, "BAD_REQUEST_ID", "invalid X-Request-Id")
            if idem_key:
                cached = self.app.db.query_one("SELECT * FROM idempotency WHERE key=?", (idem_key,))
                if cached:
                    if cached["user_id"] != (session or {}).get("user_id"):
                        raise ApiError(409, "REQUEST_ID_CONFLICT", "request id already used")
                    self._send_bytes(cached["status"], cached["body"].encode("utf-8"), "application/json; charset=utf-8")
                    return
            handler = getattr(self, route.handler)
            result = handler(session=session, **match.groupdict())
            if isinstance(result, FilePayload):
                self._send_file(result)
                return
            status, payload = result
            self._send_json(status, payload)
            if idem_key and session and status < 500:
                current = now()
                try:
                    self.app.db.execute(
                        "INSERT OR IGNORE INTO idempotency(key, user_id, method, path, status, body, "
                        "created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            idem_key,
                            session["user_id"],
                            method,
                            path,
                            status,
                            json_dumps(payload),
                            current,
                            current + 86400,
                        ),
                    )
                except sqlite3.Error:
                    pass
        except ApiError as exc:
            self._send_json(exc.status, exc.public())
        except TaskError as exc:
            self._send_json(self._task_error_status(exc.code), {"error": {"code": exc.code, "message": exc.message, "details": exc.details}})
        except PlatformError as exc:
            self._send_json(self._platform_error_status(exc.code), {"error": exc.public()})
        except ProviderError as exc:
            self._send_json(self._provider_error_status(exc.code), {"error": exc.public()})
        except SnapshotError as exc:
            self._send_json(500, {"error": {"code": "SNAPSHOT_ERROR", "message": str(exc)}})
        except BackupError as exc:
            self._send_json(400, {"error": {"code": "BACKUP_ERROR", "message": str(exc)}})
        except (ValueError, KeyError) as exc:
            self._send_json(400, {"error": {"code": "BAD_INPUT", "message": str(exc)[:500]}})
        except Exception:
            log.exception("unhandled request failure: %s %s", method, path)
            self._send_json(500, {"error": {"code": "INTERNAL", "message": "服务器内部错误，请查看日志"}})

    def _serve_static(self, name: str, content_type: str) -> None:
        base = Path(__file__).resolve().parent / "webui"
        path = (base / name).resolve()
        if base.resolve() not in path.parents or not path.is_file():
            raise ApiError(404, "NOT_FOUND", "not found")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _task_error_status(code: str) -> int:
        if code == "TASK_NOT_FOUND":
            return 404
        if code in (
            "BAD_STATE",
            "SUBMISSION_UNKNOWN",
            "DUPLICATE_SUBMISSION",
            "ACCOUNT_CANNOT_CONNECT_JUDGE",
        ):
            return 409
        if code in ("PROBLEM_IMPORT_FAILED",):
            return 502
        return 400

    @staticmethod
    def _platform_error_status(code: str) -> int:
        if code.startswith("AUTH") or code in ("NOT_CONNECTED", "ACCOUNT_CANNOT_CONNECT_JUDGE"):
            return 401
        if code == "FORBIDDEN":
            return 403
        if code == "RATE_LIMITED":
            return 429
        if code in ("NETWORK_ERROR", "PLATFORM_UNAVAILABLE"):
            return 502
        return 400

    @staticmethod
    def _provider_error_status(code: str) -> int:
        if code in ("MODEL_AUTH", "MODEL_FORBIDDEN", "MODEL_NOT_CONFIGURED"):
            return 401
        if code == "MODEL_RATE_LIMITED":
            return 429
        if code in ("MODEL_NETWORK", "MODEL_UNAVAILABLE"):
            return 502
        return 400

    # ==================================================================
    # Health / setup / auth
    # ==================================================================
    def h_health(self, **_: Any):
        return 200, {
            "ok": True,
            "product": "AscendForge",
            "version": __version__,
            "time": now(),
        }

    def h_setup_status(self, **_: Any):
        return 200, {"needs_setup": self.app.needs_setup(), "setup_token_required": True}

    def h_setup(self, **_: Any):
        if not self.app.needs_setup():
            raise ApiError(409, "ALREADY_INITIALIZED", "AscendForge 已经完成初始化")
        body = self._read_json()
        if not setup_token_valid(self.app.setup_token, str(body.get("token") or "")):
            raise ApiError(403, "SETUP_TOKEN", "初始化令牌无效，请查看服务器控制台输出")
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise ApiError(400, "BAD_USERNAME", "用户名需为 3-64 位字母、数字、下划线、点或连字符")
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            raise ApiError(400, "BAD_PASSWORD", str(exc)) from None
        user_id = new_id("user")
        self.app.db.execute(
            "INSERT INTO users(id, username, password_hash, created_at) VALUES(?, ?, ?, ?)",
            (user_id, username, password_hash, now()),
        )
        raw, csrf = create_session(
            self.app.db,
            user_id,
            self.app.settings.session_ttl,
            ip=self._client_ip(),
            user_agent=self.headers.get("User-Agent"),
        )
        self._set_session_cookie(raw)
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (username, "setup", "instance", "{}", now()),
        )
        return 200, {"authenticated": True, "user": {"username": username}, "csrf_token": csrf}

    def h_login(self, **_: Any):
        ip = self._client_ip()
        if not self.app.throttle.check(ip):
            raise ApiError(429, "LOGIN_THROTTLED", "登录尝试过于频繁，请稍后再试")
        body = self._read_json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        user = self.app.db.query_one("SELECT * FROM users WHERE username=?", (username,))
        if not user or user.get("disabled") or not verify_password(password, user["password_hash"]):
            self.app.throttle.fail(ip)
            raise ApiError(401, "LOGIN_FAILED", "用户名或密码错误")
        self.app.throttle.reset(ip)
        raw, csrf = create_session(
            self.app.db,
            user["id"],
            self.app.settings.session_ttl,
            ip=ip,
            user_agent=self.headers.get("User-Agent"),
        )
        self._set_session_cookie(raw)
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (username, "login", user["id"], "{}", now()),
        )
        return 200, {"authenticated": True, "user": {"username": username}, "csrf_token": csrf}

    def h_me(self, session: dict | None = None, **_):
        if not session:
            return 200, {"authenticated": False}
        return 200, {
            "authenticated": True,
            "user": {"username": session["username"], "id": session["user_id"]},
            "csrf_token": session["csrf_token"],
        }

    def h_logout(self, session: dict | None = None, **_):
        if session:
            revoke_session(self.app.db, session["id"])
        self._clear_session_cookie()
        return 200, {"authenticated": False}

    def h_password(self, session: dict | None = None, **_):
        body = self._read_json()
        current = str(body.get("current_password") or "")
        new = str(body.get("new_password") or "")
        user = self.app.db.query_one("SELECT * FROM users WHERE id=?", (session["user_id"],))
        if not user or not verify_password(current, user["password_hash"]):
            raise ApiError(401, "BAD_PASSWORD", "当前密码错误")
        try:
            hashed = hash_password(new)
        except ValueError as exc:
            raise ApiError(400, "BAD_PASSWORD", str(exc)) from None
        self.app.db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user["id"]))
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (user["username"], "password_change", user["id"], "{}", now()),
        )
        return 200, {"ok": True}

    # ==================================================================
    # Summary / tasks
    # ==================================================================
    def h_summary(self, **_: Any):
        return 200, self.app.summary()

    def _task_public(self, task: dict) -> dict:
        return {
            **task,
            "status_label": task["status"],
            "next_action_at": task.get("next_action_at"),
            "best_metric": task.get("best_metric_json"),
        }

    def h_tasks_list(self, **_: Any):
        items = []
        for task in self.app.tasks.list():
            items.append(
                {
                    **task,
                    "quota": self.app.scheduler.quota_snapshot(task["id"]),
                }
            )
        return 200, {"tasks": items}

    def h_tasks_create(self, session: dict | None = None, **_):
        body = self._read_json()
        task = self.app.tasks.create(
            title=str(body.get("title") or ""),
            problem_url=str(body.get("problem_url") or ""),
            config=body.get("config") if isinstance(body.get("config"), dict) else {},
            created_by=session["user_id"] if session else None,
        )
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (session["username"] if session else None, "task_create", task["id"], "{}", now()),
        )
        return self.h_task_detail(task["id"])

    def h_task_detail(self, task_id: str, **_: Any):
        detail = self.app.tasks.detail(task_id)
        detail["quota"] = self.app.scheduler.quota_snapshot(task_id)
        detail["platform"] = self.app.platform_public()
        detail["capabilities"] = self.app.capabilities()
        detail["worker"] = self.app.worker_state()
        detail["model_configured"] = self.app.credentials.has("model")
        # The UI does not need full template contents; keep a compact summary.
        template = detail.get("template") or {}
        files = template.get("files") if isinstance(template, dict) else []
        if isinstance(files, dict):
            files = [{"path": k, "content": v} for k, v in files.items()]
        detail["template"] = {
            "files": [
                {
                    "path": item.get("path"),
                    "editable": bool(item.get("editable")),
                    "bytes": len(str(item.get("content") or "").encode()),
                }
                for item in (files or [])
                if isinstance(item, dict)
            ]
        }
        problem = detail.get("problem") or {}
        if isinstance(problem, dict):
            detail["problem"] = {
                key: truncate_text(str(problem[key]), 20_000) if isinstance(problem.get(key), str) else problem.get(key)
                for key in ("_id", "name", "code_template", "description", "constraints", "score")
                if problem.get(key) is not None
            }
        return 200, {"task": detail}

    def h_task_control(self, task_id: str, action: str, session: dict | None = None, **_):
        if action == "start":
            self.app.tasks.start(task_id)
        elif action == "pause":
            self.app.tasks.pause(task_id)
        elif action == "resume":
            self.app.tasks.resume(task_id)
        elif action == "stop":
            self.app.tasks.stop(task_id)
        else:
            raise ApiError(400, "BAD_ACTION", "unknown action")
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (session["username"] if session else None, f"task_{action}", task_id, "{}", now()),
        )
        detail = self.app.tasks.detail(task_id)
        detail["quota"] = self.app.scheduler.quota_snapshot(task_id)
        return 200, {"task": detail}

    def h_baseline_submit(self, task_id: str, session: dict | None = None, **_):
        body = self._read_json()
        if body.get("confirm") is not True:
            raise ApiError(400, "CONFIRM_REQUIRED", "真实提交前必须显式确认 confirm=true")
        result = create_baseline_submission(
            self.app.db,
            self.app.settings,
            self.app.tasks,
            self.app.scheduler,
            task_id,
            actor=(session or {}).get("username", "user"),
        )
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (
                (session or {}).get("username"),
                "baseline_submit",
                task_id,
                json_dumps({"submission_id": result["submission"]["id"], "snapshot_id": result["snapshot"]["id"]}),
                now(),
            ),
        )
        return self.h_task_detail(task_id)

    def h_continue_optimization(self, task_id: str, session: dict | None = None, **_):
        task = self.app.tasks.get(task_id)
        if task["status"] != "completed" or task.get("stop_reason") != "manual_baseline_finished":
            raise ApiError(409, "BAD_STATE", "只有已完成的人工基线任务可以继续自动优化")
        if not self.app.credentials.has("model"):
            raise ApiError(409, "MODEL_NOT_CONFIGURED", "请先在“模型与设置”中配置模型 API")
        config = dict(task.get("config") or {})
        config.update(
            {
                "manual_only": False,
                "max_attempts": max(20, int(config.get("max_attempts") or 20)),
                "max_submissions_per_task": max(20, int(config.get("max_submissions_per_task") or 20)),
            }
        )
        self.app.db.execute(
            "UPDATE tasks SET status='running', stop_reason=NULL, stopped_at=NULL, next_action_at=?, "
            "config_json=?, updated_at=? WHERE id=?",
            (now(), json_dumps(config), now(), task_id),
        )
        add_event(
            self.app.db,
            task_id,
            None,
            "continue_optimization",
            {"actor": (session or {}).get("username", "user"), "previous_reason": "manual_baseline_finished"},
        )
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            ((session or {}).get("username"), "continue_optimization", task_id, "{}", now()),
        )
        return self.h_task_detail(task_id)

    def h_task_events(self, task_id: str, **_: Any):
        after = int((self.query.get("after") or ["0"])[0])
        limit = int((self.query.get("limit") or ["200"])[0])
        return 200, {"events": self.app.tasks.events(task_id, after=after, limit=limit)}

    def h_task_attempts(self, task_id: str, **_: Any):
        attempts = attempt_history(self.app.db, task_id, limit=100)
        for attempt in attempts:
            for key in ("result_json", "normalized_json", "local_validation_json"):
                if attempt.get(key):
                    attempt[key] = json_loads(attempt[key], attempt[key])
        return 200, {"attempts": attempts}

    def h_attempt_detail(self, task_id: str, attempt_id: str, **_: Any):
        attempt = get_attempt(self.app.db, attempt_id)
        if not attempt or attempt["task_id"] != task_id:
            raise ApiError(404, "ATTEMPT_NOT_FOUND", "attempt not found")
        for key in ("result_json", "normalized_json", "local_validation_json"):
            if attempt.get(key):
                attempt[key] = json_loads(attempt[key], attempt[key])
        source = []
        snapshot = None
        if attempt.get("snapshot_id"):
            bundle = read_snapshot(self.app.db, self.app.settings, attempt["snapshot_id"])
            snapshot = bundle["snapshot"]
            for path, content in sorted(bundle["files"].items()):
                source.append(
                    {
                        "path": path,
                        "content": content,
                        "sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "bytes": len(content.encode()),
                    }
                )
        parent_diff = best_diff = ""
        parent = get_attempt(self.app.db, attempt["parent_attempt_id"]) if attempt.get("parent_attempt_id") else None
        if parent and parent.get("snapshot_id") and attempt.get("snapshot_id"):
            try:
                old = read_snapshot(self.app.db, self.app.settings, parent["snapshot_id"])["files"]
                parent_diff = unified_source_diff(old, {item["path"]: item["content"] for item in source})
            except SnapshotError:
                parent_diff = "parent snapshot unavailable"
        task = self.app.tasks.get(task_id)
        if task.get("best_attempt_id") and task["best_attempt_id"] != attempt_id:
            best = get_attempt(self.app.db, task["best_attempt_id"])
            if best and best.get("snapshot_id") and attempt.get("snapshot_id"):
                try:
                    old = read_snapshot(self.app.db, self.app.settings, best["snapshot_id"])["files"]
                    best_diff = unified_source_diff(old, {item["path"]: item["content"] for item in source})
                except SnapshotError:
                    best_diff = "best snapshot unavailable"
        submission = self.app.db.query_one(
            "SELECT * FROM submissions WHERE attempt_id=? ORDER BY created_at DESC LIMIT 1", (attempt_id,)
        )
        if submission and submission.get("response_json"):
            submission["response_json"] = json_loads(submission["response_json"], submission["response_json"])
        return 200, {
            "attempt": attempt,
            "snapshot": snapshot,
            "source": source,
            "parent_diff": parent_diff,
            "best_diff": best_diff,
            "submission": submission,
        }

    def h_best_download(self, task_id: str, **_: Any):
        task = self.app.tasks.get(task_id)
        if not task.get("best_attempt_id"):
            raise ApiError(404, "NO_BEST", "任务还没有远端 best 版本")
        attempt = get_attempt(self.app.db, task["best_attempt_id"])
        if not attempt or not attempt.get("snapshot_id"):
            raise ApiError(404, "NO_SNAPSHOT", "best attempt 没有源码快照")
        bundle = read_snapshot(self.app.db, self.app.settings, attempt["snapshot_id"])
        target = build_snapshot_zip(self.app.settings, bundle["snapshot"], bundle["files"], "best")
        return FilePayload(target, self._filename(task, f"best-{attempt['ordinal']}"), "application/zip")

    def h_attempt_download(self, task_id: str, attempt_id: str, **_: Any):
        attempt = get_attempt(self.app.db, attempt_id)
        if not attempt or attempt["task_id"] != task_id or not attempt.get("snapshot_id"):
            raise ApiError(404, "ATTEMPT_NOT_FOUND", "attempt snapshot not found")
        bundle = read_snapshot(self.app.db, self.app.settings, attempt["snapshot_id"])
        target = build_snapshot_zip(self.app.settings, bundle["snapshot"], bundle["files"], f"attempt-{attempt['ordinal']}")
        task = self.app.tasks.get(task_id)
        return FilePayload(target, self._filename(task, f"attempt-{attempt['ordinal']}"), "application/zip")

    def h_task_archive(self, task_id: str, **_: Any):
        include_package = (self.query.get("include_package") or ["0"])[0] in ("1", "true", "yes")
        task = self.app.tasks.get(task_id)
        target = build_task_archive(self.app.db, self.app.settings, task_id, include_problem_package=include_package)
        return FilePayload(target, self._filename(task, "archive"), "application/zip")

    @staticmethod
    def _filename(task: dict, suffix: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", task.get("title") or "task")[:40].strip("-") or "task"
        return f"ascendforge-{task['id']}-{safe}-{suffix}.zip"

    def h_resolve_submission(self, task_id: str, submission_id: str, session: dict | None = None, **_):
        body = self._read_json()
        action = str(body.get("action") or "")
        remote_id = str(body.get("remote_id") or "").strip() or None
        note = str(body.get("note") or "")
        submission = self.app.db.query_one(
            "SELECT * FROM submissions WHERE id=? AND task_id=?", (submission_id, task_id)
        )
        if not submission:
            raise ApiError(404, "SUBMISSION_NOT_FOUND", "submission not found")
        result = self.app.scheduler.resolve_manual(
            submission_id, action=action, remote_id=remote_id, note=note, actor=(session or {}).get("username", "user")
        )
        return 200, {"result": result, "task": self.app.tasks.detail(task_id)}

    # ==================================================================
    # Model settings
    # ==================================================================
    def h_model_get(self, **_: Any):
        model = self.app.model_public()
        return 200, {
            "model": model,
            "capabilities": self.app.capabilities(),
            "skills": self.app.skills.list(),
        }

    def h_model_put(self, session: dict | None = None, **_):
        body = self._read_json()
        existing = self.app.credentials.get("model") or {}
        payload = {
            "provider": str(body.get("provider") or existing.get("provider") or "openai-compatible"),
            "base_url": str(body.get("base_url") or "").strip() or existing.get("base_url", ""),
            "model": str(body.get("model") or "").strip(),
            "api_key": str(body.get("api_key") or "").strip() or existing.get("api_key", ""),
            "supports_tool_calls": bool(body.get("supports_tool_calls", existing.get("supports_tool_calls", False))),
            "max_tokens": body.get("max_tokens", existing.get("max_tokens", 2048)),
            "temperature": body.get("temperature", existing.get("temperature", 0.2)),
            "timeout": body.get("timeout", existing.get("timeout", 120)),
            "reasoning_mode": body.get("reasoning_mode", existing.get("reasoning_mode", "auto")),
            "price_prompt_per_1k": body.get("price_prompt_per_1k", existing.get("price_prompt_per_1k")),
            "price_completion_per_1k": body.get("price_completion_per_1k", existing.get("price_completion_per_1k")),
            "updated_at": now(),
        }
        config = ModelConfig.from_payload(payload)
        payload["base_url"] = config.base_url
        payload["model"] = config.model
        payload["supports_tool_calls"] = config.supports_tool_calls
        payload["max_tokens"] = config.max_tokens
        payload["temperature"] = config.temperature
        payload["timeout"] = config.timeout
        self.app.credentials.put("model", payload)
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            ((session or {}).get("username"), "model_config_update", "model", json_dumps({"model": config.model}), now()),
        )
        return 200, {"model": self.app.model_public()}

    def h_model_delete(self, session: dict | None = None, **_):
        self.app.credentials.delete("model")
        return 200, {"model": {"configured": False}}

    def h_model_test(self, **_: Any):
        payload = self.app.credentials.get("model")
        if not payload:
            raise ApiError(400, "MODEL_NOT_CONFIGURED", "请先保存模型配置")
        config = ModelConfig.from_payload(payload)
        provider = make_provider(config)
        started = time.monotonic()
        response = provider.complete(
            [
                {"role": "system", "content": "You are a connectivity test. Reply with one short sentence."},
                {"role": "user", "content": "Reply exactly: AscendForge model connection OK"},
            ],
            tools=False,
        )
        latency = int((time.monotonic() - started) * 1000)
        return 200, {
            "ok": True,
            "model": response.model or config.model,
            "latency_ms": latency,
            "content": truncate_text(response.content, 500),
            "usage": {"prompt_tokens": response.prompt_tokens, "completion_tokens": response.completion_tokens},
            "tool_fallback": response.tool_fallback,
        }

    # ==================================================================
    # Platform
    # ==================================================================
    def h_platform_status(self, **_: Any):
        return 200, {"platform": self.app.platform_public()}

    def h_platform_captcha(self, **_: Any):
        challenge = self.app.platform.captcha()
        svg = challenge["image"].encode("utf-8")
        if len(svg) > 1024 * 1024:
            raise ApiError(502, "CAPTCHA_TOO_LARGE", "验证码过大")
        data_url = "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
        return 200, {"captcha_id": challenge["captcha_id"], "image_data_url": data_url}

    def h_platform_login(self, **_: Any):
        body = self._read_json()
        account = str(body.get("account") or "").strip()
        password = str(body.get("password") or "")
        captcha_id = str(body.get("captcha_id") or "") or None
        captcha_code = str(body.get("captcha_code") or "") or None
        problem_url = str(body.get("problem_url") or "").strip() or None
        status = self.app.platform.login(
            account,
            password,
            captcha_id=captcha_id,
            captcha_code=captcha_code,
            verify_problem_url=problem_url,
        )
        return 200, {"platform": status}

    def h_platform_import(self, **_: Any):
        body = self._read_json()
        status = self.app.platform.import_session(
            str(body.get("cookies") or ""),
            user_id=str(body.get("user_id") or "").strip() or None,
            verify_problem_url=str(body.get("problem_url") or "").strip() or None,
        )
        return 200, {"platform": status}

    def h_platform_test(self, **_: Any):
        body = self._read_json()
        target = str(body.get("problem_url") or "").strip() or DEFAULT_TEST_PROBLEM
        evidence = self.app.platform.test_connection(target)
        return 200, {"evidence": evidence}

    def h_platform_disconnect(self, **_: Any):
        return 200, {"platform": self.app.platform.disconnect()}

    # ==================================================================
    # Capabilities / skills / worker / backups
    # ==================================================================
    def h_capabilities(self, **_: Any):
        return 200, {"capabilities": self.app.capabilities()}

    def h_skills(self, **_: Any):
        return 200, {"skills": self.app.skills.list()}

    def h_worker(self, **_: Any):
        return 200, {"worker": self.app.worker_state()}

    def h_backups_list(self, **_: Any):
        return 200, {"backups": list_backups(self.app.settings)}

    def h_backups_create(self, session: dict | None = None, **_):
        body = self._read_json()
        result = create_backup(
            self.app.settings,
            self.app.db,
            include_key=bool(body.get("include_key", False)),
            include_problem_packages=bool(body.get("include_problem_packages", True)),
        )
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            ((session or {}).get("username"), "backup_create", result["path"], "{}", now()),
        )
        return 200, {"backup": result}

    def h_backup_download(self, name: str, **_: Any):
        settings = self.app.settings
        base = settings.backups_dir.resolve()
        source = (base / name).resolve()
        if base not in source.parents or not source.is_dir():
            raise ApiError(404, "BACKUP_NOT_FOUND", "backup not found")
        verification = verify_backup(source)
        if not verification["ok"]:
            raise ApiError(409, "BACKUP_INTEGRITY", "备份完整性检查失败：" + "; ".join(verification["problems"][:5]))
        target = settings.tmp_dir / f"{name}-{int(now())}.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(source).as_posix())
        return FilePayload(target, f"{name}.zip", "application/zip")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: App):
        self.app = app
        super().__init__(address, Handler)


def serve(settings: Settings) -> int:
    configure_logging(settings.log_level, settings.logs_dir)
    app = App(settings)
    try:
        server = Server((settings.host, settings.port), app)
    except OSError as exc:
        log.error("cannot bind %s:%s: %s", settings.host, settings.port, exc)
        return 1
    log.info("AscendForge web server listening on http://%s:%s", settings.host, settings.port)
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        log.info("Remote access: terminate TLS at a reverse proxy; set ASCENDFORGE_SECURE_COOKIES=true")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0
