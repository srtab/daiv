from sessions.executor.lock import Wait
from sessions.merge_request_runs.addressor import CommentsAddressorManager

from codebase.base import MergeRequest, User


def _manager(thread_id: str) -> CommentsAddressorManager:
    return CommentsAddressorManager(
        repo_id="owner/repo",
        merge_request=MergeRequest(
            repo_id="owner/repo",
            merge_request_id=42,
            source_branch="feature",
            target_branch="main",
            title="t",
            description="d",
            author=User(id=1, username="alice"),
        ),
        thread_id=thread_id,
    )


class TestLockPolicy:
    def test_every_run_waits_for_the_slot_under_its_own_id(self, stub_base_init):
        first, second = _manager("t-1")._lock_policy(), _manager("t-1")._lock_policy()

        assert isinstance(first, Wait)
        assert first.holder_id.startswith("webhook-")
        assert first.holder_id != second.holder_id


class TestClaimUnableNote:
    def test_only_the_first_claim_posts(self, stub_base_init):
        manager = _manager("t-1")

        assert manager._claim_unable_note() is True
        assert manager._claim_unable_note() is False
