"""Story 3.3 — the single shared ``still_actionable`` predicate (AC2, AC3, AC4, AC6).

Full matrix: envelope actionability (all-clear / found-issues / classifying), the live MR read
(open / draft / merged / closed), the ``UNSET`` vs explicit-``None`` envelope contract, the no-MR
short-circuit, the fail-safe (a read resolving to ``OPEN`` keeps the item), and the no-write
guarantee.
"""

import uuid
from unittest.mock import patch

import pytest
from sessions.envelopes import build_actionable_item
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin
from sessions.reconcile import still_actionable

from codebase.base import MergeRequestState

_LIVE_READ = "codebase.mr_state.get_merge_request_state"


def _make_run(*, merge_request_iid=None, repo_id="group/project"):
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id=repo_id)
    return Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id=repo_id,
        status=RunStatus.SUCCESSFUL,
        merge_request_iid=merge_request_iid,
    )


def _make_envelope(run, status, *, n_actionable=0):
    actionable = [
        build_actionable_item(id=str(i), kind="bug", label=f"issue {i}", ref="a.py") for i in range(n_actionable)
    ]
    return RunEnvelope.objects.create(run=run, status=status, actionable=actionable)


@pytest.mark.django_db
class TestClassificationActionability:
    """Layer 1: with no MR to reconcile against, the predicate mirrors envelope actionability."""

    def test_all_clear_is_not_actionable(self):
        run = _make_run()
        env = _make_envelope(run, EnvelopeStatus.ALL_CLEAR)
        assert still_actionable(run, env) is False

    def test_found_issues_is_actionable(self):
        run = _make_run()
        env = _make_envelope(run, EnvelopeStatus.FOUND_ISSUES, n_actionable=2)
        assert still_actionable(run, env) is True

    def test_needs_attention_is_actionable(self):
        run = _make_run()
        env = _make_envelope(run, EnvelopeStatus.NEEDS_ATTENTION)
        assert still_actionable(run, env) is True

    def test_failed_is_actionable(self):
        run = _make_run()
        env = _make_envelope(run, EnvelopeStatus.FAILED)
        assert still_actionable(run, env) is True

    def test_explicit_none_is_classifying_and_actionable(self):
        # An explicit ``None`` asserts "no envelope exists" (classifying) — no re-query.
        run = _make_run()
        assert still_actionable(run, None) is True

    def test_unset_looks_up_the_envelope(self):
        # The default (``_UNSET``) resolves the envelope via ``for_run``.
        run = _make_run()
        _make_envelope(run, EnvelopeStatus.ALL_CLEAR)
        assert still_actionable(run) is False

    def test_unset_with_no_envelope_is_classifying(self):
        run = _make_run()
        assert still_actionable(run) is True


@pytest.mark.django_db
class TestLiveMrResolution:
    """Layer 2: an actionable, MR-referencing run reconciles against the live cached read."""

    @pytest.mark.parametrize("state", [MergeRequestState.OPEN, MergeRequestState.DRAFT])
    def test_open_or_draft_mr_stays_actionable(self, state):
        run = _make_run(merge_request_iid=5)
        env = _make_envelope(run, EnvelopeStatus.NEEDS_ATTENTION)
        with patch(_LIVE_READ, return_value=state) as read:
            assert still_actionable(run, env) is True
        read.assert_called_once_with("group/project", 5)

    @pytest.mark.parametrize("state", [MergeRequestState.MERGED, MergeRequestState.CLOSED])
    def test_resolved_mr_is_not_actionable(self, state):
        # Merged/closed externally → resolved, leaves the console (AC4/AC5).
        run = _make_run(merge_request_iid=5)
        env = _make_envelope(run, EnvelopeStatus.FOUND_ISSUES, n_actionable=1)
        with patch(_LIVE_READ, return_value=state):
            assert still_actionable(run, env) is False

    def test_classifying_run_with_merged_mr_is_not_actionable(self):
        run = _make_run(merge_request_iid=9)
        with patch(_LIVE_READ, return_value=MergeRequestState.MERGED):
            assert still_actionable(run, None) is False

    def test_failed_read_resolves_to_open_and_keeps_the_item(self):
        # The wrapper owns the fail-safe: a failed read surfaces here as ``OPEN`` → stays (AC6).
        run = _make_run(merge_request_iid=5)
        env = _make_envelope(run, EnvelopeStatus.NEEDS_ATTENTION)
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            assert still_actionable(run, env) is True

    def test_all_clear_never_triggers_a_live_read(self):
        # A non-actionable envelope short-circuits before the MR read, even with an MR reference.
        run = _make_run(merge_request_iid=5)
        env = _make_envelope(run, EnvelopeStatus.ALL_CLEAR)
        with patch(_LIVE_READ) as read:
            assert still_actionable(run, env) is False
        read.assert_not_called()

    def test_no_merge_request_iid_skips_the_live_read(self):
        run = _make_run(merge_request_iid=None)
        env = _make_envelope(run, EnvelopeStatus.NEEDS_ATTENTION)
        with patch(_LIVE_READ) as read:
            assert still_actionable(run, env) is True
        read.assert_not_called()


@pytest.mark.django_db
class TestNoWrites:
    """AC3: the predicate is presentation-only — it mutates no stored state."""

    def test_predicate_performs_no_writes(self):
        run = _make_run(merge_request_iid=5)
        env = _make_envelope(run, EnvelopeStatus.FOUND_ISSUES, n_actionable=1)

        with (
            patch(_LIVE_READ, return_value=MergeRequestState.MERGED),
            patch.object(Run, "save") as run_save,
            patch.object(RunEnvelope, "save") as env_save,
        ):
            still_actionable(run, env)
            still_actionable(run)  # also the UNSET/for_run path

        run_save.assert_not_called()
        env_save.assert_not_called()
