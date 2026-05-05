from __future__ import annotations

from unittest.mock import patch

import pytest

from codebase.base import Issue, MergeRequest, Scope, User
from codebase.managers.base import BaseManager
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import CommentsAddressorManager
from codebase.utils import compute_thread_id


@pytest.fixture
def stub_base_init():
    """Skip BaseManager side effects (RepoClient, GitManager) — we exercise __init__ logic only."""
    with patch.object(BaseManager, "__init__", lambda self, *, runtime_ctx: setattr(self, "ctx", runtime_ctx)):
        yield


class _StubRepo:
    slug = "owner/repo"


class _StubCtx:
    repository = _StubRepo()


_AUTHOR = User(id=1, username="alice")


def _issue() -> Issue:
    return Issue(id=1, iid=42, title="t", author=_AUTHOR)


def _merge_request() -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=99,
        source_branch="b",
        target_branch="main",
        title="t",
        description="d",
        author=_AUTHOR,
    )


class TestIssueAddressorManagerThreadId:
    def test_explicit_thread_id_used(self, stub_base_init):
        manager = IssueAddressorManager(issue=_issue(), runtime_ctx=_StubCtx(), thread_id="explicit-id")
        assert manager.thread_id == "explicit-id"

    def test_none_falls_back_to_computed(self, stub_base_init):
        manager = IssueAddressorManager(issue=_issue(), runtime_ctx=_StubCtx(), thread_id=None)
        assert manager.thread_id == compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)

    def test_empty_string_rejected(self, stub_base_init):
        with pytest.raises(ValueError):
            IssueAddressorManager(issue=_issue(), runtime_ctx=_StubCtx(), thread_id="")


class TestCommentsAddressorManagerThreadId:
    def test_explicit_thread_id_used(self, stub_base_init):
        manager = CommentsAddressorManager(
            merge_request=_merge_request(), mention_comment_id="c1", runtime_ctx=_StubCtx(), thread_id="explicit-id"
        )
        assert manager.thread_id == "explicit-id"

    def test_none_falls_back_to_computed(self, stub_base_init):
        manager = CommentsAddressorManager(
            merge_request=_merge_request(), mention_comment_id="c1", runtime_ctx=_StubCtx(), thread_id=None
        )
        assert manager.thread_id == compute_thread_id(repo_slug="owner/repo", scope=Scope.MERGE_REQUEST, entity_iid=99)

    def test_empty_string_rejected(self, stub_base_init):
        with pytest.raises(ValueError):
            CommentsAddressorManager(
                merge_request=_merge_request(), mention_comment_id="c1", runtime_ctx=_StubCtx(), thread_id=""
            )
