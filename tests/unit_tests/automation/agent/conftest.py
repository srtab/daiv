from unittest.mock import AsyncMock, patch

import pytest

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
