from unittest.mock import AsyncMock, Mock, patch

import pytest

from automation.agent.context import RepoHandle
from codebase.base import MergeRequest, User


@pytest.fixture
def emit_custom_event():
    """Stub the sink ``streamed_assistant_message`` dispatches through.

    Shared so the patch target lives in one place: an ``assert_not_awaited()`` against a stale
    target passes vacuously, which is exactly the "message silently never streams" failure these
    tests exist to catch.
    """
    with patch("automation.agent.utils.adispatch_custom_event", AsyncMock()) as emit:
        yield emit


def make_merge_request(**overrides) -> MergeRequest:
    defaults = {
        "repo_id": "owner/repo",
        "merge_request_id": 42,
        "source_branch": "feature",
        "target_branch": "main",
        "title": "Test MR",
        "description": "Test description",
        "web_url": "https://example.com/owner/repo/-/merge_requests/42",
        "author": User(id=1, username="testuser"),
    }
    defaults.update(overrides)
    return MergeRequest(**defaults)


def make_repo_handle(
    slug: str, *, current_ref: str = "main", default_branch: str = "main", head_detached: bool = False
) -> RepoHandle:
    return RepoHandle(
        repo_id=slug,
        client=Mock(),
        repository=Mock(slug=slug),
        gitrepo=Mock(),
        config=Mock(default_branch=default_branch),
        ref=current_ref,
        current_ref=current_ref,
        head_detached=head_detached,
        clone_seconds=0.0,
    )
