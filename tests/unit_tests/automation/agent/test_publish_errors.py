import pytest
from git import GitCommandError
from gitlab.exceptions import GitlabCreateError

from automation.agent.publish_errors import (
    MergeRequestFailedError,
    PublishFailedError,
    PushRejectedError,
    classify,
    kind_from_exception_path,
)
from automation.agent.workspace.git_manager import GitPushNetworkError, GitPushPermissionError, GitPushStaleError


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (GitPushStaleError("stale"), PushRejectedError),
        (GitPushPermissionError("denied"), PushRejectedError),
        (GitPushNetworkError("offline"), PushRejectedError),
        (PushRejectedError("already typed"), PushRejectedError),
        (GitlabCreateError("nope"), MergeRequestFailedError),
        (GitCommandError("commit", 1), PublishFailedError),
        (RuntimeError("boom"), PublishFailedError),
    ],
)
def test_classify(exc, expected):
    assert classify(exc) is expected


def test_kinds():
    assert PushRejectedError.kind == "push_rejected"
    assert MergeRequestFailedError.kind == "mr_failed"
    assert PublishFailedError.kind == "unknown"


def test_kind_from_exception_path():
    assert kind_from_exception_path("automation.agent.publish_errors.PushRejectedError") == "push_rejected"
    assert kind_from_exception_path("automation.agent.publish_errors.MergeRequestFailedError") == "mr_failed"
    assert kind_from_exception_path("automation.agent.publish_errors.PublishFailedError") == "unknown"
    assert kind_from_exception_path("builtins.ValueError") is None
