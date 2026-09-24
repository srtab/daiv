import pytest
from sessions.executor.lock import LOCK_WAIT_TIMEOUT_S, NoLock, Wait
from sessions.models import Session, SessionOrigin

from codebase.base import Issue, User
from codebase.managers.issue_addressor import IssueAddressorManager


def _manager(thread_id: str) -> IssueAddressorManager:
    return IssueAddressorManager(
        repo_id="owner/repo",
        issue=Issue(id=1, iid=42, title="t", author=User(id=1, username="alice")),
        thread_id=thread_id,
    )


@pytest.mark.django_db(transaction=True)
class TestLockPolicy:
    async def test_it_waits_for_the_slot_of_an_existing_session(self, stub_base_init):
        await Session.objects.acreate(thread_id="t-1", origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="owner/repo")

        policy = await _manager("t-1")._lock_policy()

        assert isinstance(policy, Wait)
        assert policy.holder_id.startswith("webhook-")
        assert policy.timeout_s == LOCK_WAIT_TIMEOUT_S

    async def test_every_run_holds_the_slot_under_its_own_id(self, stub_base_init):
        await Session.objects.acreate(thread_id="t-1", origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="owner/repo")

        first, second = await _manager("t-1")._lock_policy(), await _manager("t-1")._lock_policy()

        assert first.holder_id != second.holder_id

    async def test_a_first_turn_without_a_session_row_runs_unlocked(self, stub_base_init, caplog):
        """The callback creates the row just after enqueueing the run, so the first turn may beat it; nothing else
        can hold that thread's slot yet."""
        with caplog.at_level("WARNING", logger="daiv.managers"):
            policy = await _manager("t-new")._lock_policy()

        assert policy == NoLock()
        assert "no session row" in caplog.text


class TestClaimUnableNote:
    def test_only_the_first_claim_posts(self, stub_base_init):
        manager = _manager("t-1")

        assert manager._claim_unable_note() is True
        assert manager._claim_unable_note() is False
