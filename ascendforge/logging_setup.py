"""Logging with rotation and conservative secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|authorization|password|passwd|secret|token)\s*[=:]\s*([^\s,;]+)"), r"\1=<redacted>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-<redacted>"),
    (re.compile(r"(?i)(cookie\s*:\s*)(.+)"), r"\1<redacted>"),
]


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        for pattern, replacement in _SECRET_PATTERNS:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str = "INFO", log_dir: Path | None = None, *, console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    redactor = RedactionFilter()
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "ascendforge.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
    # Third-party libraries can be noisy.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("http.client").setLevel(logging.WARNING)
