from types import SimpleNamespace

from sessions.executor.lock import Wait
from webhooks.managers.base import BaseManager
from webhooks.managers.issue_addressor import IssueAddressorManager

from codebase.base import Issue, User


def _manager(thread_id: str) -> IssueAddressorManager:
    return IssueAddressorManager(
        repo_id="owner/repo",
        issue=Issue(id=1, iid=42, title="t", author=User(id=1, username="alice")),
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


def _outcome(status: str, **extra) -> dict:
    return {"status": status, "commands": [], "rounds": 2, "suspicious_paths": [], "reason": "tests fail", **extra}


class TestVerificationFooter:
    def test_a_failed_outcome_renders_the_report_without_the_draft_clause(self):
        footer = BaseManager.verification_footer(SimpleNamespace(values={"verification": _outcome("failed")}))

        assert footer is not None
        assert "Verification" in footer
        assert "opened as a draft" not in footer

    def test_nothing_to_add_for_reported_not_configured_or_absent_outcomes(self):
        for values in (
            {"verification": _outcome("failed", reported=True)},
            {"verification": _outcome("not_configured")},
            {},
        ):
            assert BaseManager.verification_footer(SimpleNamespace(values=values)) is None
        assert BaseManager.verification_footer(None) is None
