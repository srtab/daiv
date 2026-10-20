import pytest
from sessions.executor.lock import NoLock, Wait
from sessions.models import Session, SessionOrigin
from webhooks.managers.issue_addressor import IssueAddressorManager

from codebase.base import Discussion, Issue, Note, NoteableType, User


def _manager(thread_id: str, **mention) -> IssueAddressorManager:
    return IssueAddressorManager(
        repo_id="owner/repo",
        issue=Issue(id=1, iid=42, title="t", author=User(id=1, username="alice")),
        thread_id=thread_id,
        **mention,
    )


def _discussion(*note_ids: int) -> Discussion:
    fields = {"author": User(id=2, username="bob"), "noteable_type": NoteableType.ISSUE, "system": False}
    return Discussion(id="d-1", notes=[Note(id=i, body=f"note {i}", resolvable=False, **fields) for i in note_ids])


@pytest.mark.django_db(transaction=True)
class TestLockPolicy:
    async def test_it_waits_for_the_slot_of_an_existing_session(self, stub_base_init):
        await Session.objects.acreate(thread_id="t-1", origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="owner/repo")

        policy = await _manager("t-1")._lock_policy()

        assert isinstance(policy, Wait)
        assert policy.holder_id.startswith("webhook-")

    async def test_every_run_holds_the_slot_under_its_own_id(self, stub_base_init):
        await Session.objects.acreate(thread_id="t-1", origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="owner/repo")

        first, second = await _manager("t-1")._lock_policy(), await _manager("t-1")._lock_policy()

        assert first.holder_id != second.holder_id

    async def test_a_first_turn_without_a_session_row_runs_unlocked(self, stub_base_init):
        """The callback creates the row just after enqueueing the run, so the first turn may beat it."""
        assert await _manager("t-new")._lock_policy() == NoLock()


class TestClaimUnableNote:
    def test_only_the_first_claim_posts(self, stub_base_init):
        manager = _manager("t-1")

        assert manager._claim_unable_note() is True
        assert manager._claim_unable_note() is False


class TestMentionNote:
    def test_it_picks_the_mentioning_note_among_the_replies(self, stub_base_init):
        manager = _manager("t-1", mention_comment_id="d-1", mention_note_id=12)

        assert manager._mention_note(_discussion(11, 12, 13), fallback_index=-1).id == 12

    def test_a_note_missing_from_the_discussion_raises(self, stub_base_init):
        """A deleted mention must fail the run rather than answer another note in its place."""
        manager = _manager("t-1", mention_comment_id="d-1", mention_note_id=99)

        with pytest.raises(LookupError, match="99"):
            manager._mention_note(_discussion(11, 12), fallback_index=-1)

    @pytest.mark.parametrize(("fallback_index", "expected"), [(0, 11), (-1, 13)])
    def test_a_task_queued_without_the_note_id_falls_back_to_the_index(
        self, stub_base_init, fallback_index: int, expected: int
    ):
        manager = _manager("t-1", mention_comment_id="d-1")

        assert manager._mention_note(_discussion(11, 12, 13), fallback_index=fallback_index).id == expected
