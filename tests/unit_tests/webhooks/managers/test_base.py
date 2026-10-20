import pytest
from sessions.executor.lock import Wait
from sessions.models import Session, SessionOrigin
from webhooks.managers.issue_addressor import IssueAddressorManager

from codebase.base import Issue, User


def _manager(thread_id: str) -> IssueAddressorManager:
    return IssueAddressorManager(
        repo_id="owner/repo",
        issue=Issue(id=1, iid=42, title="t", author=User(id=1, username="alice")),
        thread_id=thread_id,
    )


@pytest.mark.django_db(transaction=True)
class TestLockPolicy:
    async def test_every_run_waits_for_the_slot_under_its_own_id(self, stub_base_init):
        await Session.objects.acreate(thread_id="t-1", origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="owner/repo")

        first, second = await _manager("t-1")._lock_policy(), await _manager("t-1")._lock_policy()

        assert isinstance(first, Wait)
        assert first.holder_id.startswith("webhook-")
        assert first.holder_id != second.holder_id

    async def test_a_missing_session_row_fails_before_it_waits_for_the_slot(self, stub_base_init):
        with pytest.raises(Session.DoesNotExist):
            await _manager("t-gone")._lock_policy()


class TestClaimUnableNote:
    def test_only_the_first_claim_posts(self, stub_base_init):
        manager = _manager("t-1")

        assert manager._claim_unable_note() is True
        assert manager._claim_unable_note() is False
