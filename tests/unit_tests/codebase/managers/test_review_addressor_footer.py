"""Unit tests for the protected-branch fallback footer pipeline in CommentsAddressorManager.

The footer is a user-visible signal: when an MR push hit a protected source branch and the
publisher swapped to a fresh MR, the footer must reach reviewers in the same comment as the
agent's reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from codebase.base import GitPlatform, MergeRequest, User
from codebase.managers.review_addressor import CommentsAddressorManager

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


def _make_manager(*, platform: GitPlatform = GitPlatform.GITLAB) -> CommentsAddressorManager:
    manager = CommentsAddressorManager(repo_id="owner/repo", merge_request=_merge_request(), mention_comment_id="c1")
    manager.client.git_platform = platform
    return manager


class TestRenderProtectedBranchFooter:
    def test_renders_when_both_keys_present(self, stub_base_init):
        manager = _make_manager()
        snapshot = SimpleNamespace(
            values={"protected_branch_fallback_source": "feature", "merge_request": _new_mr_value()}
        )

        rendered = manager._render_protected_branch_footer(snapshot)

        assert rendered is not None
        assert "feature" in rendered
        assert "https://gitlab.example.com/owner/repo/-/merge_requests/200" in rendered
        assert "!200" in rendered  # GitLab-style ref

    def test_renders_github_style_for_github_platform(self, stub_base_init):
        manager = _make_manager(platform=GitPlatform.GITHUB)
        snapshot = SimpleNamespace(
            values={"protected_branch_fallback_source": "feature", "merge_request": _new_mr_value()}
        )

        rendered = manager._render_protected_branch_footer(snapshot)

        assert rendered is not None
        assert "#200" in rendered  # GitHub-style ref
        assert "pull request" in rendered  # GitHub vocabulary

    def test_returns_none_when_snapshot_is_none(self, stub_base_init):
        """A failed checkpoint read upstream surfaces as ``None``; render must short-circuit cleanly."""
        manager = _make_manager()
        assert manager._render_protected_branch_footer(None) is None

    def test_returns_none_silently_when_source_branch_missing(self, stub_base_init):
        """No fallback happened: ``merge_request`` set with no fallback source is the ordinary
        MR-scope state, not a partial checkpoint, so render must stay silent (no warning)."""
        manager = _make_manager()
        snapshot = SimpleNamespace(values={"merge_request": _new_mr_value()})
        with patch("codebase.managers.review_addressor.logger") as mock_logger:
            assert manager._render_protected_branch_footer(snapshot) is None
        mock_logger.warning.assert_not_called()

    def test_warns_when_merge_request_missing_but_source_present(self, stub_base_init):
        """A fallback source with no MR is genuinely partial/raced; surface it to the operator."""
        manager = _make_manager()
        snapshot = SimpleNamespace(values={"protected_branch_fallback_source": "feature"})
        with patch("codebase.managers.review_addressor.logger") as mock_logger:
            assert manager._render_protected_branch_footer(snapshot) is None
        mock_logger.warning.assert_called_once()

    def test_returns_none_silently_when_source_branch_empty_string(self, stub_base_init):
        """An empty source-branch is the no-fallback signal; no footer and no warning."""
        manager = _make_manager()
        snapshot = SimpleNamespace(values={"protected_branch_fallback_source": "", "merge_request": _new_mr_value()})
        with patch("codebase.managers.review_addressor.logger") as mock_logger:
            assert manager._render_protected_branch_footer(snapshot) is None
        mock_logger.warning.assert_not_called()


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
