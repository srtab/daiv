import pytest
from git import GitCommandError
from gitlab.exceptions import GitlabCreateError

from automation.agent.publish_errors import PushRejectedError, publish_error_kind
from automation.agent.workspace.git_manager import GitPushNetworkError, GitPushPermissionError, GitPushStaleError


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (GitPushStaleError("stale"), "push_rejected"),
        (GitPushPermissionError("denied"), "push_rejected"),
        (GitPushNetworkError("offline"), "push_rejected"),
        (PushRejectedError("already typed"), "push_rejected"),
        (GitlabCreateError("nope"), "mr_failed"),
        (GitCommandError("commit", 1), "unknown"),
        (RuntimeError("boom"), "unknown"),
    ],
)
def test_publish_error_kind(exc, expected):
    assert publish_error_kind(exc) == expected
