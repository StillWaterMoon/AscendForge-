"""Platform connection service: encrypted session ownership + verification."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from ..db import Database
from ..security import CredentialStore, platform_session_public
from ..util import now
from .base import ConnectionEvidence, PlatformError
from .cannjudge import CannjudgeAdapter

log = logging.getLogger(__name__)

DEFAULT_TEST_PROBLEM = "https://cannjudge.cn/hc_cann_codelabs/cann/add"


class PlatformService:
    def __init__(self, db: Database, settings: Settings, credentials: CredentialStore):
        self.db = db
        self.settings = settings
        self.credentials = credentials

    # -- storage ---------------------------------------------------------
    def _payload(self) -> dict | None:
        payload = self.credentials.get("cannjudge")
        if payload and payload.get("cookies"):
            payload.setdefault("base_url", self.settings.cannjudge_base_url)
            return payload
        return payload

    def _store(self, payload: dict) -> None:
        payload = dict(payload)
        payload["base_url"] = self.settings.cannjudge_base_url
        payload["updated_at"] = now()
        self.credentials.put("cannjudge", payload)

    def _adapter(self, payload: dict) -> CannjudgeAdapter:
        return CannjudgeAdapter(
            base_url=payload.get("base_url") or self.settings.cannjudge_base_url,
            user_id=payload.get("user_id"),
            cookies=payload.get("cookies") or [],
            max_package_mb=self.settings.max_package_mb,
        )

    def adapter(self) -> CannjudgeAdapter:
        payload = self._payload()
        if not payload or not payload.get("cookies"):
            raise PlatformError(
                "NOT_CONNECTED",
                "尚未连接 CANNJudge，请先在“平台连接”中登录账号",
                retryable=False,
                category="auth",
            )
        return self._adapter(payload)

    def save_adapter(self, adapter: CannjudgeAdapter, *, verified: bool = False) -> dict:
        payload = self._payload() or {}
        session = adapter.export_session()
        payload.update(
            {
                "base_url": adapter.base_url,
                "user_id": session.get("user_id") or payload.get("user_id"),
                "cookies": session.get("cookies") or [],
                "logged_in_at": payload.get("logged_in_at") or now(),
                "updated_at": now(),
                "last_error": None,
            }
        )
        if verified:
            payload["verified_at"] = now()
        self._store(payload)
        return payload

    # -- public state ----------------------------------------------------
    def status(self) -> dict:
        payload = self._payload()
        public = platform_session_public(payload)
        adapter_info = CannjudgeAdapter(base_url=self.settings.cannjudge_base_url).capabilities()
        public.update(
            {
                "capabilities": adapter_info,
                "base_url": self.settings.cannjudge_base_url,
                "has_saved_session": bool(payload and payload.get("cookies")),
                "judge_verified": bool(payload and payload.get("verified_at")),
                "live_verified": False,
                "notes": [
                    "接口来自公开的 CANNJudge 接入资料，尚未在本部署用真实账号完成线上提交验证。",
                    "登录成功只能证明账号可认证；能否访问具体题目在导入题目时再次校验。",
                    "任何 401/403 都会停止新的提交并把任务标记为需要重新登录/审查。",
                ],
            }
        )
        # Never expose cookies or the account in full.
        public.pop("account", None)
        account = (payload or {}).get("account")
        if account:
            public["account_masked"] = self._mask_account(account)
        return public

    @staticmethod
    def _mask_account(account: str) -> str:
        if "@" in account:
            name, _, domain = account.partition("@")
            return (name[:2] + "***@" + domain) if name else "***@" + domain
        if len(account) <= 4:
            return "***"
        return account[:3] + "****" + account[-2:]

    # -- login -----------------------------------------------------------
    def login(
        self,
        account: str,
        password: str,
        *,
        captcha_id: str | None = None,
        captcha_code: str | None = None,
        verify_problem_url: str | None = None,
    ) -> dict:
        adapter = CannjudgeAdapter(
            base_url=self.settings.cannjudge_base_url,
            max_package_mb=self.settings.max_package_mb,
        )
        try:
            result = adapter.login(
                account,
                password,
                captcha_id=captcha_id,
                captcha_code=captcha_code,
            )
        except PlatformError as exc:
            self._record_error(exc)
            raise
        payload = {
            "account": account.strip(),
            "user_id": result["user_id"],
            "cookies": result["cookies"],
            "logged_in_at": result["logged_in_at"],
            "verified_at": None,
            "last_error": None,
        }
        self._store(payload)
        # Verify judge access when the caller supplied a target problem.
        target = (verify_problem_url or "").strip()
        if target:
            evidence = self.verify(target)
            if not evidence.problem_accessible:
                # Keep the login (the user may only lack this specific problem) but
                # surface a precise error to the caller.
                error = evidence.error or {}
                raise PlatformError(
                    "ACCOUNT_CANNOT_CONNECT_JUDGE",
                    "账号可以登录，但无法连接该题目的 judge："
                    + str(error.get("message") or "题目不可访问，可能没有小组/比赛权限"),
                    retryable=False,
                    category="auth",
                    details={"code": error.get("code"), "problem_id": evidence.problem_id},
                )
        return self.status()

    def import_session(self, cookie_text: str, *, user_id: str | None = None, verify_problem_url: str | None = None) -> dict:
        cookies = self._parse_cookie_header(cookie_text)
        if not cookies:
            raise PlatformError("BAD_COOKIES", "没有解析到有效的 CANNJudge Cookie", retryable=False)
        adapter = CannjudgeAdapter(
            base_url=self.settings.cannjudge_base_url,
            user_id=user_id,
            cookies=cookies,
            max_package_mb=self.settings.max_package_mb,
        )
        if not user_id:
            raise PlatformError(
                "USER_ID_REQUIRED",
                "导入浏览器会话时必须同时提供 CANNJudge 用户 ID（登录响应中的 _id），否则无法安全提交",
                retryable=False,
            )
        self._store(
            {
                "account": "imported-session",
                "user_id": user_id,
                "cookies": adapter.export_session()["cookies"],
                "logged_in_at": now(),
                "verified_at": None,
                "last_error": None,
            }
        )
        target = (verify_problem_url or "").strip()
        if target:
            evidence = self.verify(target)
            if not evidence.problem_accessible:
                error = evidence.error or {}
                raise PlatformError(
                    "ACCOUNT_CANNOT_CONNECT_JUDGE",
                    "会话已保存，但无法连接该题目的 judge："
                    + str(error.get("message") or "题目不可访问"),
                    retryable=False,
                    category="auth",
                )
        return self.status()

    @staticmethod
    def _parse_cookie_header(text: str) -> list[dict]:
        cookies = []
        for part in (text or "").split(";"):
            if "=" not in part:
                continue
            name, _, value = part.strip().partition("=")
            if not name or not value:
                continue
            cookies.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".cannjudge.cn",
                    "path": "/",
                    "secure": True,
                    "expires": None,
                }
            )
        return cookies

    def captcha(self) -> dict:
        adapter = CannjudgeAdapter(
            base_url=self.settings.cannjudge_base_url,
            max_package_mb=self.settings.max_package_mb,
        )
        return adapter.get_captcha()

    def disconnect(self) -> dict:
        self.credentials.delete("cannjudge")
        self.db.setting_set("platform_connected", False)
        return self.status()

    # -- verification ----------------------------------------------------
    def verify(self, problem_url_or_id: str = DEFAULT_TEST_PROBLEM) -> ConnectionEvidence:
        try:
            adapter = self.adapter()
        except PlatformError as exc:
            return ConnectionEvidence(
                base_url=self.settings.cannjudge_base_url,
                reachable=False,
                authenticated=False,
                problem_accessible=False,
                error=exc.public(),
                checked_at=now(),
            )
        evidence = adapter.verify_connection(problem_url_or_id)
        payload = self._payload() or {}
        if evidence.problem_accessible:
            payload["verified_at"] = now()
            payload["last_error"] = None
            payload["cookies"] = adapter.export_session()["cookies"]
            self._store(payload)
        elif evidence.error:
            payload["last_error"] = evidence.error
            self._store(payload)
        return evidence

    def test_connection(self, problem_url: str | None = None) -> dict:
        evidence = self.verify(problem_url or self.db.setting_get("default_problem_url", DEFAULT_TEST_PROBLEM))
        return evidence.public()

    def _record_error(self, exc: PlatformError) -> None:
        payload = self._payload()
        if payload:
            payload["last_error"] = exc.public()
            self._store(payload)

    # -- problem import --------------------------------------------------
    def resolve_and_download(self, problem_url: str, task_dir: Path) -> dict:
        adapter = self.adapter()
        try:
            problem = adapter.resolve_problem(problem_url)
            problem_id = str(problem.get("_id"))
            template = adapter.get_template(problem_id)
            package = adapter.download_package(problem_id, task_dir / "problem")
        except PlatformError as exc:
            self._record_error(exc)
            if exc.code in ("AUTH_EXPIRED", "AUTH_REQUIRED"):
                raise PlatformError(
                    "ACCOUNT_CANNOT_CONNECT_JUDGE",
                    "账号无法连接 judge：登录已失效，请重新登录 CANNJudge",
                    retryable=False,
                    category="auth",
                ) from None
            if exc.code == "FORBIDDEN":
                raise PlatformError(
                    "ACCOUNT_CANNOT_CONNECT_JUDGE",
                    "账号无法连接 judge：没有该题目/小组的访问权限",
                    retryable=False,
                    category="auth",
                ) from None
            raise
        self.save_adapter(adapter, verified=True)
        return {"problem": problem, "template": template, "package": package, "adapter": adapter}

    # -- submit ----------------------------------------------------------
    def submit(self, problem: dict, template: dict, files: dict[str, str]) -> dict:
        adapter = self.adapter()
        payload = adapter.build_submission_payload(problem, template, files)
        result = adapter.submit(payload)
        self.save_adapter(adapter)
        return result

    def get_submission(self, submission_id: str) -> dict:
        adapter = self.adapter()
        result = adapter.get_submission(submission_id)
        self.save_adapter(adapter)
        return result

    def find_recent(self, problem_id: str, user_id: str, since: float) -> list[dict]:
        adapter = self.adapter()
        result = adapter.find_recent_submissions(problem_id, user_id, since)
        self.save_adapter(adapter)
        return result

    def account_user_id(self) -> str | None:
        payload = self._payload()
        return payload.get("user_id") if payload else None
