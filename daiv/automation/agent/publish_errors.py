from __future__ import annotations

from github import GithubException
from gitlab.exceptions import GitlabError
from requests import RequestException

from automation.agent.workspace.git_manager import GitPushNetworkError, GitPushPermissionError, GitPushStaleError


class PublishError(Exception):
    kind: str


class PushRejectedError(PublishError):
    kind = "push_rejected"


class MergeRequestFailedError(PublishError):
    kind = "mr_failed"


class PublishFailedError(PublishError):
    kind = "unknown"


_KIND_BY_PATH = {
    f"{cls.__module__}.{cls.__qualname__}": cls.kind
    for cls in (PushRejectedError, MergeRequestFailedError, PublishFailedError)
}


def classify(exc: BaseException) -> type[PublishError]:
    if isinstance(exc, PublishError):
        return type(exc)
    if isinstance(exc, GitPushStaleError | GitPushPermissionError | GitPushNetworkError):
        return PushRejectedError
    if isinstance(exc, GitlabError | GithubException | RequestException):
        return MergeRequestFailedError
    return PublishFailedError


def kind_from_exception_path(path: str) -> str | None:
    return _KIND_BY_PATH.get(path)
