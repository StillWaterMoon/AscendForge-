"""Remote judge platform adapters.  CANNJudge is the first implementation."""

from .base import PlatformAdapter, PlatformError, SubmissionUnknown
from .cannjudge import CannjudgeAdapter

__all__ = ["PlatformAdapter", "PlatformError", "SubmissionUnknown", "CannjudgeAdapter"]
