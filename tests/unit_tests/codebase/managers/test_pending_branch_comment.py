"""The pending-branch notice has to reach the platform comment, not just the job result.

Issue- and MR-scope runs post their reply straight from the agent's last message, so a run whose merge
request the platform refused to open would otherwise leave the person who triggered it with a normal
reply, no MR link and no explanation — indistinguishable from a run that changed nothing. Issue scope is
the primary trigger surface *and* the one where ``merge_request`` is always None, so it matters most
exactly where it was missing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codebase.base import GitPlatform, Issue, User
from codebase.managers.base import BaseManager
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.repo_config import RepositoryConfig

_AUTHOR = User(id=1, username="alice")


class _StubRepo:
    slug = "owner/repo"


@pytest.fixture
def manager() -> IssueAddressorManager:
    def _init(self, *, runtime_ctx):
        self.ctx = runtime_ctx
        self.client = MagicMock()

    with patch.object(BaseManager, "__init__", _init):
        return IssueAddressorManager(
            issue=Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=["daiv"]),
            mention_comment_id="c1",
            runtime_ctx=SimpleNamespace(
                repository=_StubRepo(),
                git_platform=GitPlatform.GITLAB,
                bot_username="daiv-bot",
                config=RepositoryConfig(),
            ),
        )


def _snapshot(**values) -> SimpleNamespace:
    return SimpleNamespace(values=values)


class TestIssueReplyNamesThePendingBranch:
    def test_appends_the_notice_to_the_reply(self, manager):
        snapshot = _snapshot(merge_request=None, pending_mr_branch="daiv/owed", pending_mr_branch_verified=True)
        body = manager._append_footer("Here's what I changed.", manager._render_footers(snapshot))

        assert body.startswith("Here's what I changed.")
        assert "daiv/owed" in body

    def test_leaves_an_ordinary_reply_untouched(self, manager):
        body = manager._append_footer("Here's what I changed.", manager._render_footers(_snapshot(merge_request=None)))

        assert body == "Here's what I changed."

    def test_tolerates_a_failed_state_read(self, manager):
        """A checkpoint read failure must not cost the user the reply itself."""
        assert manager._append_footer("Reply.", manager._render_footers(None)) == "Reply."

    def test_the_unable_note_names_the_branch_without_claiming_an_mr_exists(self, manager):
        """The recovery note is what the user reads when a run errored but its work was saved.

        ``_recover_draft`` returning "work reached the remote" must not be rendered as "created a draft
        merge request" when only a branch was pushed: there is nothing to click, and the reader goes
        looking for an MR that does not exist. The branch name has to appear instead.
        """
        manager._add_unable_to_address_issue_note(
            draft_published=False,
            fallback_footer=manager._render_pending_footer(
                _snapshot(pending_mr_branch="daiv/owed", pending_mr_branch_verified=True)
            ),
        )

        body = manager.client.create_issue_comment.call_args.args[2]
        assert "daiv/owed" in body
        assert "created a draft" not in body

    def test_the_unable_note_still_announces_a_real_draft_mr(self, manager):
        manager._add_unable_to_address_issue_note(draft_published=True)

        body = manager.client.create_issue_comment.call_args.args[2]
        assert "draft" in body

    def test_does_not_claim_safety_for_an_unconfirmed_branch(self, manager):
        snapshot = _snapshot(merge_request=None, pending_mr_branch="daiv/owed", pending_mr_branch_verified=False)
        body = manager._append_footer("Done.", manager._render_footers(snapshot))

        assert "could not confirm" in body
        assert "Nothing is lost" not in body
