"""Browser control surface: stdlib HTTP server + JSON API + static UI.

Deliberately dependency-free so the only server prerequisite is Python 3.11+ and
``cryptography``.  The browser only polls JSON; it never owns the optimization
loop.  Mutating requests support an ``X-Request-Id`` idempotency key and require
a CSRF token tied to the server-side session.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .db import Database
from .execution.capabilities import detect_capabilities
from .experiments import (
    SnapshotError,
    add_event,
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
    purge_expired_sessions,
    revoke_session,
    setup_token_valid,
    verify_password,
)
from .remote import RemoteError, RemoteStore, close_ssh_client, start_bridge_process, stop_bridge_process
from .tasks import TaskError, TaskService
from .util import canonical_json, json_dumps, json_loads, new_id, now, truncate_text
from .workload_recovery import RecoveryError, WorkloadRecoveryService

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
        self.recovery = WorkloadRecoveryService(self.db, settings, self.tasks, self.scheduler)
        self.remote = RemoteStore(self.db, self.box)
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

    def summary(self) -> dict:
        counts = self.tasks.status_counts()
        recent = self.tasks.list()[:8]
        platform = self.platform_public()
        capabilities = detect_capabilities(self.settings)
        return {
            "product": "AscendForge",
            "product_zh": "昇腾工坊",
            "version": __version__,
            "counts": counts,
            "tasks": recent,
            "worker": self.worker_state(),
            "platform": {
                "connected": platform.get("connected"),
                "judge_verified": platform.get("judge_verified"),
                "account_masked": platform.get("account_masked"),
            },
            "capabilities": {
                "npu": capabilities.get("npu", {}).get("status"),
                "cann": capabilities.get("cann", {}).get("status"),
                "remote_custom_tests": capabilities.get("remote_custom_tests", {}).get("supported"),
            },
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
            ("DELETE", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)$", "h_task_delete", False, False),
            ("POST", r"^/api/project-templates$", "h_project_template_upload", False, False),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)$", "h_task_detail", False, True),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/(?P<action>start|pause|resume|stop)$", "h_task_control", False, False),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/codex-run$", "h_codex_run", False, False),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/workload-recovery$", "h_workload_recovery_start", False, False),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/workload-recovery$", "h_workload_recovery_task", False, True),
            ("GET", r"^/api/workload-recovery/(?P<job_id>[A-Za-z0-9_-]+)$", "h_workload_recovery_get", False, True),
            ("POST", r"^/api/workload-recovery/(?P<job_id>[A-Za-z0-9_-]+)/(?P<action>pause|resume|stop)$", "h_workload_recovery_control", False, False),
            ("GET", r"^/api/workload-recovery/(?P<job_id>[A-Za-z0-9_-]+)/diagnostics$", "h_workload_recovery_diagnostics", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/attempts$", "h_task_attempts", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/attempts/(?P<attempt_id>[A-Za-z0-9_-]+)$", "h_attempt_detail", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/attempts/(?P<attempt_id>[A-Za-z0-9_-]+)/download$", "h_attempt_download", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/download/best$", "h_best_download", False, True),
            ("GET", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/archive$", "h_task_archive", False, True),
            ("POST", r"^/api/tasks/(?P<task_id>[A-Za-z0-9_-]+)/submissions/(?P<submission_id>[A-Za-z0-9_-]+)/resolve$", "h_resolve_submission", False, False),
            ("GET", r"^/api/platform/status$", "h_platform_status", False, True),
            ("GET", r"^/api/platform/captcha$", "h_platform_captcha", False, True),
            ("POST", r"^/api/platform/login$", "h_platform_login", False, False),
            ("POST", r"^/api/platform/import-session$", "h_platform_import", False, False),
            ("POST", r"^/api/platform/test$", "h_platform_test", False, False),
            ("POST", r"^/api/platform/disconnect$", "h_platform_disconnect", False, False),
            ("GET", r"^/api/remote/status$", "h_remote_status", False, True),
            ("POST", r"^/api/remote/test$", "h_remote_test", False, False),
            ("POST", r"^/api/remote/connect$", "h_remote_connect", False, False),
            ("POST", r"^/api/remote/disconnect$", "h_remote_disconnect", False, False),
            ("GET", r"^/api/capabilities$", "h_capabilities", False, True),
            ("GET", r"^/api/worker$", "h_worker", False, True),
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

    def _read_body(self, *, max_bytes: int = MAX_BODY_BYTES) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            size = int(length)
        except ValueError:
            raise ApiError(400, "BAD_BODY", "invalid Content-Length") from None
        if size <= 0:
            return b""
        if size > max_bytes:
            raise ApiError(413, "BODY_TOO_LARGE", "request body too large")
        return self.rfile.read(size)

    def _read_json(self) -> dict:
        raw = self._body_raw if self._body_raw is not None else self._read_body()
        if not raw:
            return {}
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
            # Always drain a request body, even when the route handler does not
            # parse it.  Otherwise a keep-alive client that sends `{}` on a POST
            # leaves those bytes in the socket and the next request line becomes
            # "{}GET ..." (observed as HTTP 501 on the dashboard).
            # 项目模板上传允许更大的包，其余 JSON 请求仍限制在 2 MiB。
            body_limit = (
                max(1, int(self.app.settings.max_package_mb)) * 1024 * 1024
                if path == "/api/project-templates"
                else MAX_BODY_BYTES
            )
            self._body_raw = self._read_body(max_bytes=body_limit)
            if method == "GET" and path in ("/", "/index.html"):
                self._serve_static("index.html", "text/html; charset=utf-8")
                return
            if method == "GET" and path.startswith("/static/"):
                name = path[len("/static/") :]
                if name not in ("app.js", "style.css", "assets/ascendforge-logo.png", "assets/ascendforge-mark.png"):
                    raise ApiError(404, "NOT_FOUND", "not found")
                if name.endswith(".js"):
                    content_type = "application/javascript; charset=utf-8"
                elif name.endswith(".css"):
                    content_type = "text/css; charset=utf-8"
                elif name.endswith(".svg"):
                    content_type = "image/svg+xml"
                else:
                    content_type = "image/png"
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
        except RecoveryError as exc:
            status = 404 if exc.code == "RECOVERY_NOT_FOUND" else 409 if exc.code in ("RECOVERY_LOCKED", "RECOVERY_REQUIRES_PASS_BASELINE", "BAD_STATE") else 400
            self._send_json(status, {"error": {"code": exc.code, "message": exc.message, "details": exc.details}})
        except SnapshotError as exc:
            self._send_json(500, {"error": {"code": "SNAPSHOT_ERROR", "message": str(exc)}})
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

    def h_project_template_upload(self, session: dict | None = None, **_):
        """接收浏览器上传的项目模板 ZIP，只做存储和基础校验。

        真正的安全解压发生在任务创建时。这里先限制原始大小、ZIP 文件数
        和解压后大小，避免把一个几十 GB 的 zip bomb 留在磁盘上。
        """
        raw = self._body_raw if self._body_raw is not None else b""
        max_bytes = max(1, int(self.app.settings.max_package_mb)) * 1024 * 1024
        if not raw:
            raise ApiError(400, "EMPTY_UPLOAD", "请选择一个 ZIP 项目模板")
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            raise ApiError(400, "BAD_ZIP", "项目模板必须是有效的 ZIP 文件")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                if not infos:
                    raise ApiError(400, "EMPTY_UPLOAD", "ZIP 中没有文件")
                if len(infos) > 500:
                    raise ApiError(400, "TOO_MANY_FILES", "ZIP 文件数量超过 500")
                total = sum(int(info.file_size) for info in infos)
                if total > max(64 * 1024 * 1024, max_bytes * 4):
                    raise ApiError(413, "ZIP_TOO_LARGE", "ZIP 解压后过大")
                for info in infos:
                    name = info.filename.replace("\\", "/")
                    if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
                        raise ApiError(400, "BAD_ZIP_PATH", "ZIP 包含非法路径")
        except zipfile.BadZipFile:
            raise ApiError(400, "BAD_ZIP", "项目模板 ZIP 已损坏") from None
        upload_id = new_id("upload")
        upload_dir = self.app.settings.tmp_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"{upload_id}.zip"
        target.write_bytes(raw)
        filename = (self.headers.get("X-Filename") or "project-template.zip").strip()[:120]
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (
                session["username"] if session else None,
                "project_template_upload",
                upload_id,
                canonical_json({"bytes": len(raw), "files": len(infos)}),
                now(),
            ),
        )
        return 200, {
            "upload_id": upload_id,
            "filename": filename,
            "bytes": len(raw),
            "file_count": len(infos),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def h_tasks_create(self, session: dict | None = None, **_):
        body = self._read_json()
        execution_mode = str(body.get("execution_mode") or "codex").strip().lower()
        if execution_mode != "codex":
            raise ApiError(400, "API_MODE_REMOVED", "LLM API 执行模式已移除；任务必须使用本地 CLI")
        upload_id = str(body.get("project_upload_id") or "").strip()
        project_archive = None
        if upload_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", upload_id):
                raise ApiError(400, "BAD_UPLOAD_ID", "项目模板上传 ID 非法")
            project_archive = self.app.settings.tmp_dir / "uploads" / f"{upload_id}.zip"
            if not project_archive.is_file():
                raise ApiError(400, "UPLOAD_NOT_FOUND", "项目模板上传已过期，请重新上传")
        config = body.get("config") if isinstance(body.get("config"), dict) else {}
        config = dict(config)
        config["execution_mode"] = execution_mode
        if execution_mode == "codex":
            config["cli_backend"] = str(body.get("cli_backend") or config.get("cli_backend") or "codex").strip().lower()
            if config["cli_backend"] != "codex":
                raise ApiError(400, "CLI_BACKEND_NOT_AVAILABLE", f"CLI 运行器 {config['cli_backend']} 尚未接入")
        task = self.app.tasks.create(
            title=str(body.get("title") or ""),
            problem_url=str(body.get("problem_url") or ""),
            config=config,
            created_by=session["user_id"] if session else None,
            project_archive=project_archive,
        )
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (
                session["username"] if session else None,
                "task_create",
                task["id"],
                json_dumps({"execution_mode": execution_mode}),
                now(),
            ),
        )
        codex_prompt = str(body.get("codex_prompt") or "").strip()
        if len(codex_prompt) > 8000:
            raise ApiError(400, "BAD_PROMPT", "Codex 初始指令不能超过 8000 字符")
        task_detail = self.h_task_detail(task["id"])[1]["task"]
        if execution_mode == "codex":
            try:
                task_detail["codex_run"] = self._launch_codex_runner(task["id"], codex_prompt, session)
            except Exception as exc:
                task_detail["codex_start_error"] = str(exc)
                add_event(self.app.db, task["id"], None, "codex_auto_start_failed", {"error": str(exc)})
            refreshed = self.app.tasks.get(task["id"])
            task_detail["config"] = refreshed.get("config") or {}
        return 200, {"task": task_detail}

    def h_task_detail(self, task_id: str, **_: Any):
        self.app.tasks.reconcile_codex_runner(task_id)
        detail = self.app.tasks.detail(task_id)
        detail["quota"] = self.app.scheduler.quota_snapshot(task_id)
        detail["budget"] = self.app.scheduler.budget_report(self.app.tasks.get(task_id))
        detail["workload_recovery"] = self.app.recovery.summary_for_task(task_id)
        detail["platform"] = self.app.platform_public()
        detail["capabilities"] = self.app.capabilities()
        detail["worker"] = self.app.worker_state()
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
                for key in ("_id", "name", "code_template", "description", "desc", "constraints", "score")
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

    def h_task_delete(self, task_id: str, session: dict | None = None, **_):
        warnings = self.app.recovery.cleanup_for_task(task_id)
        result = self.app.tasks.delete(
            task_id,
            actor=(session or {}).get("username", "user"),
        )
        if warnings:
            result["warnings"] = warnings
        return 200, result

    def _launch_codex_runner(self, task_id: str, prompt: str, session: dict | None) -> dict:
        """启动一个 Codex app-server 会话（Web 按钮和创建任务共用）。"""
        task = self.app.tasks.get(task_id)
        # 先把任务切换到 running，再启动子进程；否则前端在 child 尚未
        # 完成 tasks.start 时点“暂停任务”会收到 pending/BAD_STATE。
        if task["status"] == "pending":
            task = self.app.tasks.start(task_id)
        elif task["status"] == "paused":
            task = self.app.tasks.resume(task_id)
        elif task["status"] != "running":
            raise ApiError(400, "BAD_STATE", f"任务当前为 {task['status']}，不能启动 Codex")
        config = dict(task.get("config") or {})
        if config.get("codex_runner_active"):
            raise ApiError(409, "CODEX_ALREADY_RUNNING", "该任务已有 Codex 运行中")
        config["execution_mode"] = "codex"
        config["codex_runner_active"] = True
        runner_token = new_id("codexrun")
        config["codex_runner_token"] = runner_token
        log_path = self.app.settings.logs_dir / f"codex-{task_id}-{int(now())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "ascendforge",
            "codex-loop",
            "--task",
            task_id,
            "--lock-token",
            runner_token,
        ]
        if prompt:
            command.extend(["--prompt", prompt])
        config["codex_runner_log"] = str(log_path)
        config["codex_runner_status"] = "starting"
        config["codex_pause_requested"] = False
        config["codex_pause_phase"] = None
        config["codex_resume_phase"] = "resuming_session" if config.get("codex_session_ref") else "starting_session"
        config["codex_resume_request_id"] = new_id("coderesume")
        config["codex_resume_started_at"] = now()
        config["codex_phase_updated_at"] = now()
        add_event(
            self.app.db,
            task_id,
            None,
            "codex_resume_requested",
            {
                "request_id": config["codex_resume_request_id"],
                "thread_id": config.get("codex_session_ref"),
                "mode": "new_process",
            },
        )
        self.app.db.execute(
            "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
            (json_dumps(config), now(), task_id),
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.app.settings.root)
        env["ASCENDFORGE_DATA_DIR"] = str(self.app.settings.data_dir)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        handle = open(log_path, "a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.app.settings.root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=flags,
            )
        except Exception:
            config.pop("codex_runner_active", None)
            config.pop("codex_runner_log", None)
            config.pop("codex_runner_status", None)
            config.pop("codex_runner_token", None)
            self.app.db.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (json_dumps(config), now(), task_id),
            )
            raise
        finally:
            handle.close()
        config["codex_runner_pid"] = process.pid
        config["codex_runner_started_at"] = now()
        config["codex_runner_status"] = "running"
        self.app.db.execute(
            "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
            (json_dumps(config), now(), task_id),
        )
        add_event(
            self.app.db,
            task_id,
            None,
            "codex_run_started",
            {"pid": process.pid, "mode": "continuous_until_manual_pause", "log": str(log_path), "actor": (session or {}).get("username")},
        )
        return {
            "started": True,
            "pid": process.pid,
            "mode": "continuous_until_manual_pause",
            "log": str(log_path),
            "task": self.app.tasks.get(task_id),
        }

    def h_codex_run(self, task_id: str, session: dict | None = None, **_):
        self.app.tasks.reconcile_codex_runner(task_id)
        task = self.app.tasks.get(task_id)
        if task["status"] not in ("running", "paused", "pending"):
            if task["status"] not in ("completed", "stopped"):
                raise ApiError(400, "BAD_STATE", "Codex 只能在 pending/paused/running/completed/stopped 任务上启动")
            self.app.db.execute(
                "UPDATE tasks SET status='paused', stop_reason=NULL, stopped_at=NULL, paused_at=?, updated_at=? WHERE id=?",
                (now(), now(), task_id),
            )
            add_event(self.app.db, task_id, None, "codex_run_reopened", {"previous_status": task["status"]})
            task = self.app.tasks.get(task_id)
        current_config = dict(task.get("config") or {})
        if task["status"] == "running" and current_config.get("codex_pause_requested"):
            # 暂停还在 requested 阶段时用户又点继续：取消暂停请求，不重启 runner。
            current_config["codex_pause_requested"] = False
            current_config["codex_pause_phase"] = None
            current_config["codex_runner_status"] = "running"
            self.app.db.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (json_dumps(current_config), now(), task_id),
            )
            add_event(self.app.db, task_id, None, "codex_pause_cancelled", {})
            return 200, {"started": True, "resumed_existing": True, "task": self.app.tasks.get(task_id)}
        if task["status"] == "paused" and current_config.get("codex_runner_active"):
            # 软暂停：runner 和 app-server 还活着，直接把任务恢复成 running，
            # 由原 runner 在同一个 thread 上继续下一轮。
            request_id = new_id("coderesume")
            current_config["codex_pause_requested"] = False
            current_config["codex_pause_phase"] = None
            current_config["codex_runner_status"] = "running"
            current_config["codex_resume_phase"] = "resuming_session"
            current_config["codex_resume_request_id"] = request_id
            current_config["codex_resume_started_at"] = now()
            current_config["codex_phase_updated_at"] = now()
            self.app.db.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (json_dumps(current_config), now(), task_id),
            )
            self.app.tasks.resume(task_id)
            add_event(
                self.app.db,
                task_id,
                None,
                "codex_resume_requested",
                {
                    "request_id": request_id,
                    "thread_id": current_config.get("codex_session_ref"),
                    "mode": "soft",
                },
            )
            add_event(
                self.app.db,
                task_id,
                None,
                "codex_soft_resume",
                {"pid": current_config.get("codex_runner_pid"), "request_id": request_id},
            )
            return 200, {
                "started": True,
                "resumed_existing": True,
                "task": self.app.tasks.get(task_id),
            }
        body = self._read_json()
        prompt = str(body.get("prompt") or "").strip()
        if len(prompt) > 8000:
            raise ApiError(400, "BAD_PROMPT", "prompt 不能超过 8000 字符")
        return 200, self._launch_codex_runner(task_id, prompt, session)

    def h_workload_recovery_start(self, task_id: str, session: dict | None = None, **_):
        body = self._read_json()
        force = bool(body.get("force", False))
        result = self.app.recovery.start(task_id, actor=(session or {}).get("username"), force=force)
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            (
                (session or {}).get("username"),
                "workload_recovery_start",
                result.get("job", {}).get("id") if result.get("job") else task_id,
                json_dumps({"mode": result.get("mode"), "force": force}),
                now(),
            ),
        )
        return 200, result

    def h_workload_recovery_task(self, task_id: str, **_: Any):
        return 200, {"recovery": self.app.recovery.summary_for_task(task_id)}

    def h_workload_recovery_get(self, job_id: str, **_: Any):
        job = self.app.recovery.get_job(job_id)
        return 200, {
            "job": self.app.recovery.job_public(job),
            "diagnostics": self.app.recovery.diagnostics(job_id),
        }

    def h_workload_recovery_control(self, job_id: str, action: str, session: dict | None = None, **_):
        if action == "pause":
            self.app.recovery.pause(job_id)
        elif action == "resume":
            self.app.recovery.resume(job_id)
        else:
            self.app.recovery.stop(job_id)
        job = self.app.recovery.get_job(job_id)
        self.app.db.insert(
            "INSERT INTO audit_log(actor, action, target, detail_json, created_at) VALUES(?, ?, ?, ?, ?)",
            ((session or {}).get("username"), f"workload_recovery_{action}", job_id, "{}", now()),
        )
        return 200, {"job": self.app.recovery.job_public(job)}

    def h_workload_recovery_diagnostics(self, job_id: str, **_: Any):
        return 200, {"diagnostics": self.app.recovery.diagnostics(job_id)}

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
    # Remote execution bridge
    # ==================================================================
    def h_remote_status(self, **_: Any):
        return 200, {"remote": self.app.remote.public()}

    def h_remote_test(self, **_: Any):
        body = self._read_json()
        previous = self.app.remote.get()
        if previous:
            stop_bridge_process(previous.get("pid"))
        try:
            self.app.remote.save(body)
            client = self.app.remote.connect_client()
            try:
                client.exec_command("true", timeout=10)[1].channel.recv_exit_status()
            finally:
                close_ssh_client(client)
        except RemoteError as exc:
            raise ApiError(400, "REMOTE_CONNECT_FAILED", str(exc)) from None
        except Exception as exc:
            raise ApiError(502, "REMOTE_CONNECT_FAILED", f"SSH 连接失败：{exc}") from None
        return 200, {"remote": self.app.remote.public(), "tested": True}

    def h_remote_connect(self, **_: Any):
        body = self._read_json()
        previous = self.app.remote.get()
        if previous:
            stop_bridge_process(previous.get("pid"))
        try:
            self.app.remote.save(body)
        except RemoteError as exc:
            raise ApiError(400, "REMOTE_CONFIG_INVALID", str(exc)) from None
        try:
            self.app.remote.set_status(
                status="connecting", pid=None, bridge_port=None, bridge_token=None, last_error=None
            )
            process = start_bridge_process(self.app.settings)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                time.sleep(0.25)
                row = self.app.remote.get()
                if row and row["status"] in ("connected", "error"):
                    break
                if process.poll() is not None:
                    break
        except Exception as exc:
            stop_bridge_process(process.pid if "process" in locals() else None)
            self.app.remote.set_status(status="error", last_error=str(exc)[:1000])
            raise ApiError(502, "REMOTE_CONNECT_FAILED", f"无法启动 SSH 桥：{exc}") from None
        row = self.app.remote.get()
        if not row or row["status"] != "connected":
            stop_bridge_process(process.pid)
            message = (row or {}).get("last_error") or "SSH 桥未在限定时间内连接"
            self.app.remote.set_status(status="error", last_error=message)
            raise ApiError(502, "REMOTE_CONNECT_FAILED", message)
        return 200, {"remote": self.app.remote.public()}

    def h_remote_disconnect(self, **_: Any):
        row = self.app.remote.get()
        if row:
            stop_bridge_process(row.get("pid"))
            self.app.remote.set_status(
                status="disconnected",
                pid=None,
                bridge_port=None,
                bridge_token=None,
                last_error=None,
            )
        return 200, {"remote": self.app.remote.public()}

    # ==================================================================
    # Capabilities / skills / worker
    # ==================================================================
    def h_capabilities(self, **_: Any):
        return 200, {"capabilities": self.app.capabilities()}

    def h_worker(self, **_: Any):
        return 200, {"worker": self.app.worker_state()}


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
