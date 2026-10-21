"""CANNJudge adapter.

The endpoints below are taken from publicly documented CANNJudge integration
material (the ``cannjudge-submit`` skill in ``hicann/cann-learning-hub``,
README last reviewed 2026-10; see docs/capability-matrix.md).  They have *not*
yet been exercised with a live account from this installation, so the UI and
capability matrix label live submit/query as "documented, not live-verified".

Design rules:
* Never guess an endpoint.  If the platform changes, fail with a clear error.
* A submit POST is never retried automatically.  Timeouts and missing ids are
  reported as ``SubmissionUnknown`` so the scheduler can reconcile.
* Cookies are credentials.  They live only in this adapter and in the encrypted
  credential store, never in task snapshots, prompts or ordinary logs.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from ..util import atomic_write, now
from .base import ConnectionEvidence, PlatformAdapter, PlatformError, SubmissionUnknown

USER_AGENT = "AscendForge/0.1 (server-side operator optimization; admin-configured)"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ERROR_BYTES = 4096

FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

# Documented forbidden files for npu_kernel_dev projects.
FORBIDDEN_KERNEL_FILES = {"judge.asc", "main.asc", "data_utils.h"}
FORBIDDEN_PATH_PARTS = {".git", "__pycache__", "build", "out", "logs"}


class CannjudgeAdapter(PlatformAdapter):
    name = "cannjudge"

    def __init__(
        self,
        base_url: str = "https://cannjudge.cn",
        *,
        user_id: str | None = None,
        cookies: list[dict] | None = None,
        timeout: int = 30,
        max_package_mb: int = 64,
    ):
        base_url = (base_url or "https://cannjudge.cn").rstrip("/")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https":
            raise PlatformError(
                "INSECURE_BASE_URL",
                "CANNJudge must be reached over HTTPS (http is only allowed for localhost tests)",
                retryable=False,
            )
        self.base_url = base_url
        self.user_id = user_id
        self.timeout = max(5, int(timeout))
        self.max_package_bytes = max(1, int(max_package_mb)) * 1024 * 1024
        self.jar = http.cookiejar.CookieJar()
        self.ssl_context = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self.ssl_context),
        )
        if cookies:
            self.load_cookies(cookies)

    # ------------------------------------------------------------------
    # Session / cookies
    # ------------------------------------------------------------------
    def load_cookies(self, cookies: list[dict]) -> None:
        for item in cookies or []:
            try:
                name = str(item["name"])
                value = str(item["value"])
            except (KeyError, TypeError):
                continue
            if not name:
                continue
            domain = str(item.get("domain") or "cannjudge.cn")
            path = str(item.get("path") or "/")
            try:
                expires = int(item["expires"]) if item.get("expires") not in (None, "") else None
            except (TypeError, ValueError):
                expires = None
            cookie = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=bool(item.get("secure", True)),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            self.jar.set_cookie(cookie)

    def export_session(self) -> dict:
        cookies = []
        for cookie in self.jar:
            if cookie.is_expired():
                continue
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": bool(cookie.secure),
                    "expires": cookie.expires,
                }
            )
        return {
            "base_url": self.base_url,
            "user_id": self.user_id,
            "cookies": cookies,
            "logged_in_at": now(),
        }

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _headers(self, extra: dict | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": self.base_url,
            "Referer": self.base_url + "/home",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        timeout: int | None = None,
        raw: bool = False,
        max_bytes: int = MAX_JSON_BYTES,
        extra_headers: dict | None = None,
    ) -> tuple[int, dict, bytes]:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
        data = None
        headers = self._headers(extra_headers)
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                status = int(response.status)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                body = self._read_limited(response, max_bytes)
                return status, response_headers, body
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = self._read_limited(exc, max_bytes)
            except Exception:
                pass
            status = int(exc.code)
            response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            detail = self._extract_error(body)
            code, message, retryable = self._map_http_error(status, detail)
            raise PlatformError(
                code,
                message,
                retryable=retryable,
                http_status=status,
                details={"path": path, "retry_after": response_headers.get("retry-after")},
            ) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise PlatformError(
                "NETWORK_ERROR",
                f"无法连接 CANNJudge：{str(reason)[:200]}",
                retryable=True,
                details={"path": path},
            ) from None
        finally:
            self._persist_cookie_header()

    def _persist_cookie_header(self) -> None:
        # The cookie jar updates automatically.  This hook exists so a caller can
        # persist the session immediately after a request when needed.
        return None

    @staticmethod
    def _read_limited(stream: Any, limit: int) -> bytes:
        chunks = []
        remaining = limit
        while remaining > 0:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _extract_error(body: bytes) -> str:
        text = body[:MAX_ERROR_BYTES].decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except ValueError:
            return text.strip()[:400]
        if isinstance(payload, dict):
            for key in ("message", "msg", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:400]
            if isinstance(payload.get("data"), dict):
                for key in ("message", "msg", "error"):
                    value = payload["data"].get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:400]
        return text.strip()[:400]

    @staticmethod
    def _map_http_error(status: int, detail: str) -> tuple[str, str, bool]:
        if status == 401:
            return "AUTH_EXPIRED", "CANNJudge 登录已失效，请重新连接账号", False
        if status == 403:
            return "FORBIDDEN", "CANNJudge 拒绝访问：账号可能没有该题目/小组权限", False
        if status == 429:
            return "RATE_LIMITED", "CANNJudge 触发限流，请稍后重试", True
        if status >= 500:
            return "PLATFORM_UNAVAILABLE", f"CANNJudge 服务异常（HTTP {status}）", True
        if status == 404:
            return "NOT_FOUND", "CANNJudge 返回 404：题目或提交不存在，或接口已变更", False
        return "PLATFORM_REJECTED", f"CANNJudge 拒绝请求（HTTP {status}）：{detail}", False

    def _json(self, method: str, path: str, **kwargs) -> Any:
        _, _, body = self._request(method, path, **kwargs)
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PlatformError(
                "BAD_RESPONSE",
                "CANNJudge 返回了无法解析的响应，接口可能已变更",
                retryable=False,
                details={"path": path},
            ) from exc

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], (dict, list)):
            return payload["data"]
        return payload

    # ------------------------------------------------------------------
    # Capabilities / connectivity
    # ------------------------------------------------------------------
    def capabilities(self) -> dict:
        return {
            "platform": "cannjudge",
            "base_url": self.base_url,
            "login": {"status": "documented_endpoint_verified", "methods": ["account_password", "session_cookie"], "captcha": True},
            "problem_import": {"status": "live_verified_anonymous", "supports": ["problem_url", "problem_id"]},
            "template": {"status": "live_verified_anonymous"},
            "package_download": {"status": "live_verified_anonymous"},
            "submit": {"status": "documented_endpoint_verified_not_submitted", "modes": ["npu_kernel_dev", "registry"]},
            "query": {"status": "documented_endpoint_verified"},
            "rank": {"status": "documented"},
            "custom_tests": {"status": "unknown", "supported": False},
            "evidence": [
                "https://github.com/hicann/cann-learning-hub/tree/master/skills/cannjudge-submit",
                "https://cannjudge.cn/home",
                "2026-10-21 live: GET /api/groups/name/hc_cann_codelabs, /api/contests/group/{id}, "
                "/api/problems/contest/{id}, /api/problems/{id}, /api/problems/{id}/template, "
                "/api/problems/{id}/package returned 200; template listed 8 files; package ZIP 16357 bytes.",
                "2026-10-21 live: GET /api/users/captcha returned captchaId+SVG; POST /api/users/login "
                "returned 400 'password required'; POST /api/submissions/submit returned 400 "
                "'userId and problemId required' — endpoints exist, authentication not yet exercised.",
            ],
            "notes": [
                "Public problem/template/package access was live-verified on 2026-10-21 without an account.",
                "Login, real submit and result query still require the user's account and a live submission.",
                "The documented /api/submissions/problem/{id}/latest endpoint returned 404 live; reconciliation "
                "uses the frontend-verified /api/submissions/user/{userId}/problem/{problemId} endpoint.",
                "No automatic retry of submit POST; unknown outcomes are reconciled first.",
                "Custom test-data upload support is unverified and therefore disabled.",
            ],
        }

    def verify_connection(self, problem_url_or_id: str, *, fetch_template: bool = True) -> ConnectionEvidence:
        evidence = ConnectionEvidence(
            base_url=self.base_url,
            reachable=False,
            authenticated=bool(self.user_id and len(self.jar)),
            problem_accessible=False,
            user_id=self.user_id,
            checked_at=now(),
        )
        try:
            problem = self.resolve_problem(problem_url_or_id)
            evidence.reachable = True
            evidence.checks.append({"step": "resolve_problem", "ok": True, "problem_id": problem.get("_id")})
            evidence.problem_id = problem.get("_id")
            evidence.problem_title = problem.get("name") or problem.get("title")
            evidence.code_template = problem.get("code_template")
            if fetch_template:
                template = self.get_template(str(problem.get("_id")))
                files = self._template_files(template)
                evidence.checks.append({"step": "get_template", "ok": True, "files": len(files)})
            evidence.problem_accessible = True
            evidence.authenticated = bool(self.user_id)
        except PlatformError as exc:
            evidence.error = exc.public()
            evidence.reachable = exc.code not in ("NETWORK_ERROR", "INSECURE_BASE_URL")
            evidence.authenticated = bool(self.user_id) and exc.code != "AUTH_EXPIRED"
            evidence.checks.append({"step": "verify", "ok": False, "code": exc.code, "http_status": exc.http_status})
        return evidence

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def get_captcha(self) -> dict:
        payload = self._json("GET", "/api/users/captcha")
        data = self._unwrap(payload)
        if not isinstance(data, dict) or not data.get("captchaId") or not isinstance(data.get("image"), str):
            raise PlatformError("BAD_CAPTCHA", "CANNJudge 未返回有效验证码", retryable=False)
        return {"captcha_id": str(data["captchaId"]), "image": data["image"]}

    def login(
        self,
        account: str,
        password: str,
        *,
        login_type: str | None = None,
        captcha_id: str | None = None,
        captcha_code: str | None = None,
    ) -> dict:
        account = (account or "").strip()
        if not account or not password:
            raise PlatformError("AUTH_INPUT", "账号和密码不能为空", retryable=False)
        self.jar.clear()
        self.user_id = None
        account_type = login_type or ("phone" if re.fullmatch(r"\d{6,15}", account) else "email")
        payload: dict[str, Any] = {"account": account, "loginType": account_type, "password": password}
        if account_type == "email":
            payload["email"] = account
        if bool(captcha_id) != bool(captcha_code):
            raise PlatformError("CAPTCHA_INCOMPLETE", "验证码 ID 和内容必须同时提供", retryable=False)
        if captcha_id:
            payload["captchaId"] = captcha_id
            payload["captchaCode"] = captcha_code
        try:
            response = self._json("POST", "/api/users/login", json_body=payload, timeout=self.timeout)
        except PlatformError as exc:
            if exc.code in ("AUTH_EXPIRED", "PLATFORM_REJECTED") and exc.http_status in (400, 401, 422):
                raise PlatformError(
                    "AUTH_FAILED",
                    "CANNJudge 登录失败：账号/密码错误、需要验证码，或该账号无法连接 judge",
                    retryable=False,
                    http_status=exc.http_status,
                    details={"hint": "检查账号是否能登录 cannjudge.cn 并访问目标题目"},
                ) from None
            raise
        user = self._unwrap(response)
        user_id = None
        if isinstance(user, dict):
            user_id = user.get("_id") or user.get("id")
        if not user_id:
            raise PlatformError(
                "AUTH_BAD_RESPONSE",
                "CANNJudge 登录响应缺少用户 ID，无法确认账号已连接 judge",
                retryable=False,
            )
        self.user_id = str(user_id)
        if not len(self.jar):
            raise PlatformError(
                "NO_SESSION_COOKIE",
                "CANNJudge 登录未返回会话 Cookie，无法保持连接",
                retryable=False,
            )
        return {"user_id": self.user_id, "account": account, "logged_in_at": now(), "cookies": self.export_session()["cookies"]}

    # ------------------------------------------------------------------
    # Problems
    # ------------------------------------------------------------------
    def get_problem(self, problem_id: str) -> dict:
        if not problem_id:
            raise PlatformError("BAD_PROBLEM", "题目 ID 不能为空", retryable=False)
        payload = self._json(
            "GET",
            f"/api/problems/{urllib.parse.quote(str(problem_id), safe='')}",
            params={"userId": self.user_id} if self.user_id else None,
        )
        data = self._unwrap(payload)
        if not isinstance(data, dict) or not (data.get("_id") or data.get("id")):
            raise PlatformError("BAD_PROBLEM", "CANNJudge 返回的题目数据无效", retryable=False)
        if not data.get("_id"):
            data["_id"] = data["id"]
        return data

    def resolve_problem(self, problem_url_or_id: str) -> dict:
        value = (problem_url_or_id or "").strip()
        if not value:
            raise PlatformError("BAD_PROBLEM", "请提供题目链接或题目 ID", retryable=False)
        if not value.startswith(("http://", "https://")):
            return self.get_problem(value)
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in ("cannjudge.cn", "www.cannjudge.cn"):
            raise PlatformError(
                "BAD_PROBLEM_URL",
                "题目链接必须来自 https://cannjudge.cn",
                retryable=False,
            )
        raw_path = parsed.fragment.split("?")[0] if parsed.fragment.startswith("/") else parsed.path
        parts = [urllib.parse.unquote(p) for p in raw_path.strip("/").split("/") if p]
        if len(parts) < 3:
            raise PlatformError(
                "BAD_PROBLEM_URL",
                "请提供包含小组、比赛和题目名称的完整题目链接",
                retryable=False,
            )
        group_name, contest_name, problem_name = parts[:3]
        if group_name == "public":
            group = self._unwrap(self._json("GET", "/api/groups/public"))
        else:
            group = self._unwrap(
                self._json("GET", "/api/groups/name/" + urllib.parse.quote(group_name, safe=""))
            )
        if not isinstance(group, dict) or not group.get("_id"):
            raise PlatformError("BAD_PROBLEM_URL", "无法定位题目所属小组", retryable=False)
        contests = self._unwrap(self._json("GET", f"/api/contests/group/{group['_id']}"))
        if isinstance(contests, dict):
            contests = contests.get("contests") or []
        matches = [
            c
            for c in (contests or [])
            if isinstance(c, dict) and (c.get("name") == contest_name or c.get("_id") == contest_name)
        ]
        if len(matches) != 1:
            raise PlatformError("BAD_PROBLEM_URL", "无法在指定小组唯一定位比赛", retryable=False)
        contest_id = matches[0]["_id"]
        params = {"contestId": contest_id}
        if self.user_id:
            params["userId"] = self.user_id
        try:
            problem = self._unwrap(
                self._json(
                    "GET",
                    "/api/problems/name/" + urllib.parse.quote(problem_name, safe=""),
                    params=params,
                )
            )
        except PlatformError as exc:
            if exc.code != "NOT_FOUND":
                raise
            # The public page slug and the API canonical name can drift (for
            # example /add on the page vs name "addv2" in the API).  Resolve via
            # the contest problem list only when the slug matches exactly one
            # canonical name or title; never guess an arbitrary problem.
            problems = self._unwrap(self._json("GET", f"/api/problems/contest/{contest_id}"))
            if isinstance(problems, dict):
                problems = problems.get("problems") or problems.get("list") or []
            matches = []
            for candidate in problems or []:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("contest_id") not in (None, contest_id):
                    continue
                identifiers = {
                    str(candidate.get(key) or "")
                    for key in ("name", "canonical_name", "title")
                }
                if problem_name in identifiers:
                    matches.append(candidate)
            if len(matches) == 1:
                problem = matches[0]
            elif len(matches) > 1:
                raise PlatformError(
                    "AMBIGUOUS_PROBLEM",
                    "题目链接在该比赛下匹配到多个题目，请使用更精确的链接或题目 ID",
                    retryable=False,
                ) from None
            else:
                available = [
                    str(item.get("canonical_name") or item.get("name") or item.get("_id"))
                    for item in (problems or [])
                    if isinstance(item, dict)
                ][:20]
                raise PlatformError(
                    "PROBLEM_NOT_FOUND",
                    "题目不存在或链接已变更；该比赛可用题目：" + ", ".join(available),
                    retryable=False,
                ) from None
        if not isinstance(problem, dict) or not problem.get("_id"):
            raise PlatformError("BAD_PROBLEM", "CANNJudge 未返回题目数据", retryable=False)
        if problem.get("contest_id") and problem.get("contest_id") != contest_id:
            raise PlatformError("BAD_PROBLEM", "题目返回的比赛与链接不一致", retryable=False)
        return problem

    def get_template(self, problem_id: str) -> dict:
        payload = self._json(
            "GET",
            f"/api/problems/{urllib.parse.quote(str(problem_id), safe='')}/template",
            params={"userId": self.user_id} if self.user_id else None,
        )
        data = self._unwrap(payload)
        if not isinstance(data, dict):
            return {"files": []}
        if "files" not in data and isinstance(data.get("data"), dict):
            data = data["data"]
        return data

    @staticmethod
    def _template_files(template: dict) -> list[dict]:
        files = template.get("files") if isinstance(template, dict) else None
        if isinstance(files, dict):
            files = [{"path": k, "content": v} for k, v in files.items()]
        result = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("name")
            if not path:
                continue
            result.append(
                {
                    "path": str(path),
                    "content": item.get("content") if isinstance(item.get("content"), str) else "",
                    "editable": bool(item.get("editable", False)),
                }
            )
        return result

    def download_package(self, problem_id: str, target_dir: str | Path) -> dict:
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        status, headers, body = self._request(
            "GET",
            f"/api/problems/{urllib.parse.quote(str(problem_id), safe='')}/package",
            params={"userId": self.user_id} if self.user_id else None,
            timeout=max(60, self.timeout),
            raw=True,
            max_bytes=self.max_package_bytes,
            extra_headers={"Accept": "application/zip, application/octet-stream, */*"},
        )
        if not body.startswith(b"PK"):
            raise PlatformError(
                "BAD_PACKAGE",
                "CANNJudge 未返回 ZIP 工程包，接口可能已变更",
                retryable=False,
                details={"content_type": headers.get("content-type", "")},
            )
        archive = target_dir / "package.zip"
        atomic_write(archive, body)
        project_dir = target_dir / "project"
        extracted = self.extract_package(archive, project_dir)
        return {
            "archive": str(archive),
            "project_dir": str(project_dir),
            "bytes": len(body),
            "files": extracted,
            "sha256": __import__("hashlib").sha256(body).hexdigest(),
        }

    @staticmethod
    def extract_package(archive: Path, target_dir: Path) -> list[dict]:
        target_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[dict] = []
        total = 0
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise PlatformError("BAD_PACKAGE", "工程包包含越界路径，已拒绝解压", retryable=False)
                parts = name.split("/")
                if any(part in ("", ".", "..") for part in parts):
                    raise PlatformError("BAD_PACKAGE", "工程包包含非法路径，已拒绝解压", retryable=False)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise PlatformError("BAD_PACKAGE", "工程包包含符号链接，已拒绝解压", retryable=False)
                total += info.file_size
                if total > 256 * 1024 * 1024:
                    raise PlatformError("BAD_PACKAGE", "工程包解压后过大，已拒绝", retryable=False)
                destination = (target_dir / name).resolve()
                if target_dir.resolve() not in destination.parents:
                    raise PlatformError("BAD_PACKAGE", "工程包越界，已拒绝解压", retryable=False)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(destination, "wb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        dst.write(chunk)
                import hashlib

                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                extracted.append({"path": name, "bytes": info.file_size, "sha256": digest})
        return extracted

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    def build_submission_payload(
        self,
        problem: dict,
        template: dict,
        files: dict[str, str],
        *,
        user_id: str | None = None,
    ) -> dict:
        user_id = user_id or self.user_id
        if not user_id:
            raise PlatformError("AUTH_REQUIRED", "提交前必须先连接 CANNJudge 账号", retryable=False)
        problem_id = str(problem.get("_id") or problem.get("id"))
        code_template = str(problem.get("code_template") or "registry")
        cleaned: dict[str, str] = {}
        for path, content in files.items():
            normal = str(path).replace("\\", "/")
            if normal.startswith("/") or ".." in normal.split("/"):
                raise PlatformError("BAD_FILE_PATH", f"非法文件路径：{path}", retryable=False)
            if not FILE_NAME_RE.match(normal.split("/")[-1]):
                raise PlatformError("BAD_FILE_NAME", f"非法文件名：{path}", retryable=False)
            if not isinstance(content, str):
                raise PlatformError("BAD_FILE_CONTENT", f"文件内容必须是文本：{path}", retryable=False)
            if len(content.encode("utf-8")) > 2 * 1024 * 1024:
                raise PlatformError("FILE_TOO_LARGE", f"文件过大：{path}", retryable=False)
            cleaned[normal] = content
        if code_template == "npu_kernel_dev":
            payload_files = self._kernel_files(template, cleaned)
            return {"problemId": problem_id, "userId": user_id, "files": payload_files}
        return {"problemId": problem_id, "userId": user_id, **self._registry_fields(cleaned)}

    @staticmethod
    def _kernel_files(template: dict, files: dict[str, str]) -> list[dict]:
        template_files = CannjudgeAdapter._template_files(template)
        editable = {f["path"]: f["content"] for f in template_files if f.get("editable")}
        readonly = {f["path"]: f["content"] for f in template_files if not f.get("editable")}
        if not editable:
            raise PlatformError(
                "TEMPLATE_EMPTY",
                "题目模板没有可编辑文件，无法安全构造提交",
                retryable=False,
            )
        missing = [path for path in editable if path not in files]
        if missing:
            raise PlatformError(
                "MISSING_EDITABLE_FILES",
                "提交缺少模板要求的可编辑文件：" + ", ".join(sorted(missing)[:10]),
                retryable=False,
            )
        # A submitted file may not silently overwrite a read-only template file.
        changed_readonly = [
            path for path, original in readonly.items() if path in files and files[path] != original
        ]
        if changed_readonly:
            raise PlatformError(
                "READONLY_MODIFIED",
                "只读模板文件被修改，拒绝提交：" + ", ".join(sorted(changed_readonly)[:10]),
                retryable=False,
            )
        payload: list[dict] = []
        for path in sorted(files):
            if path in readonly:
                continue
            if path in FORBIDDEN_KERNEL_FILES:
                raise PlatformError("FORBIDDEN_FILE", f"平台禁止提交文件：{path}", retryable=False)
            if any(part in FORBIDDEN_PATH_PARTS for part in path.split("/")):
                raise PlatformError("FORBIDDEN_FILE", f"平台禁止提交该路径：{path}", retryable=False)
            if path not in editable:
                suffix = Path(path).suffix.lower()
                if suffix not in (".asc", ".h") or "/" in path:
                    raise PlatformError(
                        "EXTRA_FILE_REJECTED",
                        f"核函数工程只允许额外提交根目录 .asc/.h 文件：{path}",
                        retryable=False,
                    )
            payload.append({"path": path, "content": files[path]})
        return payload

    @staticmethod
    def _registry_fields(files: dict[str, str]) -> dict:
        def only_one(paths: list[str], label: str) -> str:
            if not paths:
                raise PlatformError(
                    "MISSING_REGISTRY_FILES",
                    f"缺少传统算子工程文件：{label}",
                    retryable=False,
                )
            if len(paths) > 1:
                raise PlatformError(
                    "AMBIGUOUS_REGISTRY_FILES",
                    f"{label} 存在多个候选文件，无法安全确定提交字段："
                    + ", ".join(sorted(paths)[:8]),
                    retryable=False,
                )
            return files[paths[0]]

        tiling_paths = [
            path
            for path in files
            if Path(path).name.endswith("_tiling.h") and not Path(path).name.startswith("tiling_key_")
        ]
        key_paths = [path for path in files if Path(path).name.startswith("tiling_key_") and path.endswith(".h")]
        host_paths = [path for path in files if path.endswith(".cpp") and "op_host" in path.split("/")]
        kernel_paths = [path for path in files if path.endswith(".cpp") and "op_kernel" in path.split("/")]

        tiling_h = only_one(tiling_paths, "*_tiling.h")
        if len(key_paths) > 1:
            raise PlatformError("AMBIGUOUS_REGISTRY_FILES", "存在多个 tiling_key_*.h", retryable=False)
        tiling_key_h = files[key_paths[0]] if key_paths else ""
        host_cpp = only_one(host_paths, "op_host/*.cpp")
        if kernel_paths:
            kernel_cpp = only_one(kernel_paths, "op_kernel/*.cpp")
        else:
            remaining = [
                path
                for path in files
                if path.endswith(".cpp") and path not in host_paths
            ]
            kernel_cpp = only_one(remaining, "kernel_cpp")
        return {
            "tiling_h": tiling_h,
            "tiling_key_h": tiling_key_h,
            "host_cpp": host_cpp,
            "kernel_cpp": kernel_cpp,
        }

    def submit(self, payload: dict) -> dict:
        if not self.user_id:
            raise PlatformError("AUTH_REQUIRED", "提交前必须先连接 CANNJudge 账号", retryable=False)
        try:
            response = self._json(
                "POST",
                "/api/submissions/submit",
                json_body=payload,
                timeout=self.timeout,
            )
        except PlatformError as exc:
            if exc.http_status and exc.http_status >= 500:
                raise SubmissionUnknown(
                    "CANNJudge 提交时服务端返回 5xx，无法确认是否已受理",
                    details={"http_status": exc.http_status, "error": exc.public()},
                ) from None
            if exc.code in ("NETWORK_ERROR", "BAD_RESPONSE"):
                raise SubmissionUnknown(
                    "提交请求超时/中断或响应无法解析，远端可能已受理，必须先对账",
                    details={"error": exc.public()},
                ) from None
            raise
        data = self._unwrap(response)
        submission_id = None
        if isinstance(data, dict):
            submission_id = data.get("submissionId") or data.get("_id") or data.get("id")
        if not submission_id:
            raise SubmissionUnknown(
                "CANNJudge 提交响应没有 submissionId，无法确认是否已受理",
                details={"response_keys": sorted(response.keys()) if isinstance(response, dict) else []},
            )
        return {"submission_id": str(submission_id), "raw": response}

    def get_submission(self, submission_id: str) -> dict:
        payload = self._json(
            "GET",
            f"/api/submissions/{urllib.parse.quote(str(submission_id), safe='')}",
            params={"userId": self.user_id} if self.user_id else None,
            timeout=self.timeout,
        )
        data = self._unwrap(payload)
        if not isinstance(data, dict):
            raise PlatformError("BAD_RESPONSE", "CANNJudge 返回的提交结果无效", retryable=False)
        return data

    def find_recent_submissions(self, problem_id: str, user_id: str, since: float) -> list[dict]:
        """List this account's submissions for one problem.

        The documented ``/api/submissions/problem/{id}/latest`` endpoint returned
        404 when probed live on 2026-10-21.  The CANNJudge frontend uses
        ``/api/submissions/user/{userId}/problem/{problemId}`` (and the user list
        as fallback); both require the authenticated session.
        """
        quoted_user = urllib.parse.quote(str(user_id), safe="")
        quoted_problem = urllib.parse.quote(str(problem_id), safe="")
        candidates = [
            (f"/api/submissions/user/{quoted_user}/problem/{quoted_problem}", None),
            (f"/api/submissions/user/{quoted_user}", {"withCount": 1, "limit": 50, "order": "desc"}),
        ]
        data: Any = []
        last_error: PlatformError | None = None
        for path, params in candidates:
            try:
                payload = self._json("GET", path, params=params, timeout=self.timeout)
                data = self._unwrap(payload)
                break
            except PlatformError as exc:
                last_error = exc
                if exc.code in ("AUTH_EXPIRED", "AUTH_REQUIRED", "FORBIDDEN"):
                    raise
                continue
        else:
            if last_error:
                raise last_error
            return []
        if isinstance(data, dict):
            data = data.get("list") or data.get("submissions") or []
        if not isinstance(data, list):
            return []
        results = []
        for item in data:
            if not isinstance(item, dict):
                continue
            item_user = str(item.get("user_id") or item.get("userId") or "")
            if item_user and item_user != str(user_id):
                continue
            item_problem = str(item.get("problem_id") or (item.get("problem") or {}).get("_id") or "")
            if item_problem and item_problem != str(problem_id):
                continue
            submitted_at = self._parse_timestamp(
                item.get("created_at")
                or item.get("createdAt")
                or item.get("submitted_at")
                or item.get("submit_time")
                or item.get("create_time")
            )
            if submitted_at is not None and submitted_at < since - 300:
                continue
            results.append(item)
        return results

    @staticmethod
    def _parse_timestamp(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            return number / 1000.0 if number > 1e12 else number
        if isinstance(value, str):
            text = value.strip().replace("Z", "+00:00")
            try:
                from datetime import datetime

                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                return None
        return None
