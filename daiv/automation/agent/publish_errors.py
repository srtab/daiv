from __future__ import annotations

from automation.agent.workspace.git_manager import GitPushNetworkError, GitPushPermissionError, GitPushStaleError
from codebase.clients.base import PLATFORM_ERRORS


class PushRejectedError(Exception):
    """A push the remote refused."""


def publish_error_kind(exc: BaseException) -> str:
    """The ``sessions.models.PublishErrorKind`` value a publish that raised ``exc`` reports."""
    if isinstance(exc, PushRejectedError | GitPushStaleError | GitPushPermissionError | GitPushNetworkError):
        return "push_rejected"
    if isinstance(exc, PLATFORM_ERRORS):
        return "mr_failed"
    return "unknown"
