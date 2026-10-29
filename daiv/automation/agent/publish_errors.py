from __future__ import annotations

from github import GithubException
from gitlab.exceptions import GitlabError
from requests import RequestException

from automation.agent.workspace.git_manager import GitPushNetworkError, GitPushPermissionError, GitPushStaleError


class PushRejectedError(Exception):
    kind = "push_rejected"


def publish_error_kind(exc: BaseException) -> str:
    """The ``sessions.models.PublishErrorKind`` value a publish that raised ``exc`` reports."""
    if isinstance(exc, PushRejectedError | GitPushStaleError | GitPushPermissionError | GitPushNetworkError):
        return PushRejectedError.kind
    if isinstance(exc, GitlabError | GithubException | RequestException):
        return "mr_failed"
    return "unknown"
