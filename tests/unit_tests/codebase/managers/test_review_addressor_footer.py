"""Unit tests for the protected-branch fallback footer pipeline in CommentsAddressorManager.

The footer is a user-visible signal: when an MR push hit a protected source branch and the
publisher swapped to a fresh MR, the footer must reach reviewers in the same comment as the
agent's reply. Previously untested; regressions here would silently drop the notice or
(worse) tear down the reply path entirely on a checkpointer hiccup.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from redis.exceptions import RedisError

from codebase.base import GitPlatform, MergeRequest, User
from codebase.managers.base import BaseManager
from codebase.managers.review_addressor import CommentsAddressorManager


@pytest.fixture
def stub_base_init():
    """Skip BaseManager side effects (RepoClient, GitManager); exercise manager methods directly."""
    with patch.object(BaseManager, "__init__", lambda self, *, runtime_ctx: setattr(self, "ctx", runtime_ctx)):
        yield


class _StubRepo:
    slug = "owner/repo"


def _ctx(*, platform: GitPlatform = GitPlatform.GITLAB) -> SimpleNamespace:
    return SimpleNamespace(repository=_StubRepo(), git_platform=platform, bot_username="daiv-bot")


_AUTHOR = User(id=1, username="alice")


def _merge_request(merge_request_id: int = 99) -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=merge_request_id,
        source_branch="feature",
        target_branch="main",
        title="t",
        description="d",
        author=_AUTHOR,
    )


def _new_mr_value(merge_request_id: int = 200) -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=merge_request_id,
        source_branch="agent/fresh-branch",
        target_branch="main",
        title="agent",
        description="d",
        author=_AUTHOR,
        web_url="https://gitlab.example.com/owner/repo/-/merge_requests/200",
    )


def _make_manager(stub_base_init, *, platform=GitPlatform.GITLAB) -> CommentsAddressorManager:
    return CommentsAddressorManager(
        merge_request=_merge_request(), mention_comment_id="c1", runtime_ctx=_ctx(platform=platform)
    )


class TestRenderProtectedBranchFooter:
    def test_renders_when_both_keys_present(self, stub_base_init):
        manager = _make_manager(stub_base_init)
        snapshot = SimpleNamespace(
            values={"protected_branch_fallback_source": "feature", "merge_request": _new_mr_value()}
        )

        rendered = manager._render_protected_branch_footer(snapshot)

        assert rendered is not None
        assert "feature" in rendered
        assert "https://gitlab.example.com/owner/repo/-/merge_requests/200" in rendered
        assert "!200" in rendered  # GitLab-style ref

    def test_renders_github_style_for_github_platform(self, stub_base_init):
        manager = _make_manager(stub_base_init, platform=GitPlatform.GITHUB)
        snapshot = SimpleNamespace(
            values={"protected_branch_fallback_source": "feature", "merge_request": _new_mr_value()}
        )

        rendered = manager._render_protected_branch_footer(snapshot)

        assert rendered is not None
        assert "#200" in rendered  # GitHub-style ref
        assert "pull request" in rendered  # GitHub vocabulary

    def test_returns_none_when_snapshot_is_none(self, stub_base_init):
        """A failed checkpoint read upstream surfaces as ``None``; render must short-circuit cleanly."""
        manager = _make_manager(stub_base_init)
        assert manager._render_protected_branch_footer(None) is None

    def test_returns_none_when_source_branch_missing(self, stub_base_init):
        manager = _make_manager(stub_base_init)
        snapshot = SimpleNamespace(values={"merge_request": _new_mr_value()})
        assert manager._render_protected_branch_footer(snapshot) is None

    def test_returns_none_when_merge_request_missing(self, stub_base_init):
        manager = _make_manager(stub_base_init)
        snapshot = SimpleNamespace(values={"protected_branch_fallback_source": "feature"})
        assert manager._render_protected_branch_footer(snapshot) is None

    def test_returns_none_when_source_branch_empty_string(self, stub_base_init):
        """An empty source-branch is the no-fallback signal; no footer should render."""
        manager = _make_manager(stub_base_init)
        snapshot = SimpleNamespace(values={"protected_branch_fallback_source": "", "merge_request": _new_mr_value()})
        assert manager._render_protected_branch_footer(snapshot) is None


class TestAppendFooter:
    def test_returns_body_unchanged_when_footer_is_none(self):
        assert CommentsAddressorManager._append_footer("the body", None) == "the body"

    def test_returns_body_unchanged_when_footer_is_empty(self):
        assert CommentsAddressorManager._append_footer("the body", "") == "the body"

    def test_separates_body_and_footer_with_blank_line(self):
        result = CommentsAddressorManager._append_footer("the body", "the footer")
        assert result == "the body\n\nthe footer"

    def test_strips_trailing_whitespace_from_body(self):
        """Stops a trailing newline from compounding into a triple-newline that
        breaks the GitLab/GitHub markdown renderer."""
        result = CommentsAddressorManager._append_footer("body\n\n\n", "footer")
        assert result == "body\n\nfooter"

    def test_strips_leading_whitespace_from_footer(self):
        result = CommentsAddressorManager._append_footer("body", "\n\nfooter")
        assert result == "body\n\nfooter"

    def test_handles_empty_body(self):
        """An empty body + footer must not produce a leading blank line that some
        markdown renderers swallow as front-matter."""
        result = CommentsAddressorManager._append_footer("", "footer")
        assert result == "\n\nfooter"


class TestSafeGetState:
    """``_safe_get_state`` must (1) return ``None`` on transport / serialization failure
    so the reply path keeps working, and (2) NOT swallow programming errors that the
    narrowed exception list excludes."""

    async def test_returns_state_on_success(self, stub_base_init):
        manager = _make_manager(stub_base_init)
        agent = Mock()
        snapshot = SimpleNamespace(values={})
        agent.aget_state = AsyncMock(return_value=snapshot)

        result = await manager._safe_get_state(agent, {"configurable": {}})

        assert result is snapshot

    async def test_returns_none_on_redis_error(self, stub_base_init):
        manager = _make_manager(stub_base_init)
        agent = Mock()
        agent.aget_state = AsyncMock(side_effect=RedisError("connection refused"))

        result = await manager._safe_get_state(agent, {"configurable": {}})

        assert result is None

    async def test_returns_none_on_oserror(self, stub_base_init):
        """Network-level failures (host down, TCP reset) surface as OSError."""
        manager = _make_manager(stub_base_init)
        agent = Mock()
        agent.aget_state = AsyncMock(side_effect=OSError("network unreachable"))

        result = await manager._safe_get_state(agent, {"configurable": {}})

        assert result is None

    async def test_returns_none_on_json_decode_error(self, stub_base_init):
        """Checkpoint payload corruption (non-JSON content) must not crash the reply path."""
        manager = _make_manager(stub_base_init)
        agent = Mock()
        agent.aget_state = AsyncMock(side_effect=json.JSONDecodeError("bad", "<doc>", 0))

        result = await manager._safe_get_state(agent, {"configurable": {}})

        assert result is None

    async def test_does_not_swallow_unrelated_exceptions(self, stub_base_init):
        """Programming errors (KeyError, TypeError) must propagate so they're caught
        by tests and CI rather than degrading silently in production."""
        manager = _make_manager(stub_base_init)
        agent = Mock()
        agent.aget_state = AsyncMock(side_effect=KeyError("checkpoint key missing"))

        with pytest.raises(KeyError):
            await manager._safe_get_state(agent, {"configurable": {}})
