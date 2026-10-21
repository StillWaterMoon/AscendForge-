"""Platform adapter contract.

The adapter is intentionally independent of the Agent: it returns structured
results, preserves the platform's raw payload, and never exposes credentials to
the Agent runtime.  Only CANNJudge is implemented in v1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class PlatformError(RuntimeError):
    """Structured platform failure.

    ``code`` values are stable and safe to show in the UI.  ``retryable`` only
    describes whether a later attempt *might* work; the scheduler still decides.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        details: dict | None = None,
        category: str = "platform",
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.details = details or {}
        self.category = category

    def public(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "category": self.category,
            "details": self.details,
        }


class SubmissionUnknown(PlatformError):
    """The submit request may have been accepted remotely (e.g. timeout)."""

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__("SUBMISSION_UNKNOWN", message, retryable=False, details=details, category="submission")


@dataclass(slots=True)
class ConnectionEvidence:
    base_url: str
    reachable: bool
    authenticated: bool
    problem_accessible: bool
    user_id: str | None = None
    problem_id: str | None = None
    problem_title: str | None = None
    code_template: str | None = None
    checks: list[dict] = field(default_factory=list)
    error: dict | None = None
    checked_at: float = 0.0

    def public(self) -> dict:
        return {
            "base_url": self.base_url,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "problem_accessible": self.problem_accessible,
            "user_id": self.user_id,
            "problem_id": self.problem_id,
            "problem_title": self.problem_title,
            "code_template": self.code_template,
            "checks": self.checks,
            "error": self.error,
            "checked_at": self.checked_at,
        }


class PlatformAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def capabilities(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def login(self, account: str, password: str, **kwargs) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_captcha(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def resolve_problem(self, problem_url_or_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_problem(self, problem_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_template(self, problem_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def download_package(self, problem_id: str, target_dir) -> dict:
        raise NotImplementedError

    @abstractmethod
    def submit(self, payload: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_submission(self, submission_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def find_recent_submissions(self, problem_id: str, user_id: str, since: float) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def export_session(self) -> dict:
        raise NotImplementedError
