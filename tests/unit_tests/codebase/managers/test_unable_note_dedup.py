"""Regression guard: a single agent-invocation failure must post exactly ONE
"unable to address" note, not two.

Both addressor entry points wrap the agent invocation in nested ``except Exception``
handlers — an inner one inside ``_address_*`` that recovers any draft and posts an
*informed* note, and an outer one in the ``address_*`` classmethod that is a catch-all
for failures occurring *before* the agent runs. When ``ainvoke`` itself raises, both
handlers used to fire, posting the error note twice (two identical comments on the MR /
issue). The note must be idempotent per manager instance so the user sees it once.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase.base import GitPlatform, Issue, MergeRequest, User
from codebase.managers.base import BaseManager
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import CommentsAddressorManager

_AUTHOR = User(id=1, username="alice")


class _StubRepo:
    slug = "owner/repo"


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        repository=_StubRepo(), git_platform=GitPlatform.GITLAB, bot_username="daiv-bot", config=MagicMock()
    )


@asynccontextmanager
async def _noop_checkpointer():
    yield MagicMock()


@pytest.fixture
def captured_client():
    """Stub ``BaseManager.__init__`` so every manager instance shares one client mock the
    test can inspect, and the resolved kwargs feed the (patched) downstream agent stack."""
    client = MagicMock()

    def _init(self, *, runtime_ctx):
        self.ctx = runtime_ctx
        self.client = client
        self.store = MagicMock()

    with patch.object(BaseManager, "__init__", _init):
        yield client


_AGENT_KWARGS = {"model_names": ["m"], "thinking_level": "medium"}


def _failing_agent() -> MagicMock:
    agent = MagicMock()
    agent.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    return agent


class TestReviewNotePostedOnce:
    async def test_agent_failure_posts_single_note(self, captured_client):
        captured_client.get_merge_request_comment.return_value = SimpleNamespace(
            notes=[SimpleNamespace(author=SimpleNamespace(username="bob"), id="n1", body="hi")]
        )
        merge_request = MergeRequest(
            repo_id="owner/repo",
            merge_request_id=99,
            source_branch="feature",
            target_branch="main",
            title="t",
            description="d",
            author=_AUTHOR,
        )

        with (
            patch("codebase.managers.review_addressor.open_checkpointer", _noop_checkpointer),
            patch("codebase.managers.review_addressor.get_daiv_agent_kwargs", return_value=_AGENT_KWARGS),
            patch("codebase.managers.review_addressor.create_daiv_agent", AsyncMock(return_value=_failing_agent())),
            patch("codebase.managers.review_addressor.build_langsmith_config", return_value={"configurable": {}}),
            patch("codebase.managers.review_addressor.track_usage_metadata", MagicMock()),
            patch.object(CommentsAddressorManager, "_recover_draft", AsyncMock(return_value=False)),
            patch.object(CommentsAddressorManager, "_safe_get_state", AsyncMock(return_value=None)),
            pytest.raises(RuntimeError),
        ):
            await CommentsAddressorManager.address_comments(
                merge_request=merge_request, mention_comment_id="c1", runtime_ctx=_ctx()
            )

        assert captured_client.create_merge_request_comment.call_count == 1


class TestIssueNotePostedOnce:
    async def test_agent_failure_posts_single_note(self, captured_client):
        issue = Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=[])

        with (
            patch("codebase.managers.issue_addressor.open_checkpointer", _noop_checkpointer),
            patch("codebase.managers.issue_addressor.get_daiv_agent_kwargs", return_value=_AGENT_KWARGS),
            patch("codebase.managers.issue_addressor.create_daiv_agent", AsyncMock(return_value=_failing_agent())),
            patch("codebase.managers.issue_addressor.build_langsmith_config", return_value={"configurable": {}}),
            patch("codebase.managers.issue_addressor.track_usage_metadata", MagicMock()),
            patch.object(IssueAddressorManager, "_recover_draft", AsyncMock(return_value=False)),
            pytest.raises(RuntimeError),
        ):
            await IssueAddressorManager.address_issue(issue=issue, runtime_ctx=_ctx())

        assert captured_client.create_issue_comment.call_count == 1
