import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

import pytest
from notifications.choices import EnvelopeStatus
from notifications.models import Notification
from notifications.policy import (
    NOTIFY_WORTHY_STATUSES,
    RECLASSIFY_MAX_AGE,
    STATUS_TONES,
    envelope_tone,
    notification_source_for_inbox_pause,
    notification_source_for_watch,
    notifies,
    notify_worthy,
    status_severity,
    within_relevance_window,
)
from sessions.models import EventKind, EventState, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import make_event


def test_notify_worthy_truth_table():
    assert notify_worthy(EnvelopeStatus.FOUND_ISSUES) is True
    assert notify_worthy(EnvelopeStatus.NEEDS_ATTENTION) is True
    assert notify_worthy(EnvelopeStatus.NEEDS_INPUT) is True
    assert notify_worthy(EnvelopeStatus.FAILED) is True
    assert notify_worthy(EnvelopeStatus.ALL_CLEAR) is False


def test_a_question_renders_amber():
    assert envelope_tone(EnvelopeStatus.NEEDS_INPUT) == "warning"


def test_window_rejects_none_and_too_old():
    assert within_relevance_window(None) is False
    assert within_relevance_window(timezone.now() - RECLASSIFY_MAX_AGE - timedelta(hours=1)) is False


def test_window_accepts_recent():
    assert within_relevance_window(timezone.now() - timedelta(minutes=5)) is True


def test_status_severity_ranks_worst_first():
    ranks = [status_severity(status) for status in EnvelopeStatus.worst_first()]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(EnvelopeStatus)


def test_status_severity_sorts_an_unranked_status_first():
    # Sorting it last would put it first in line for the cap to drop, which is the one outcome a
    # reader cannot detect. Being seen out of order is recoverable; being hidden is not.
    assert status_severity("brand-new") < status_severity(EnvelopeStatus.FAILED)


class TestEveryStatusIsTriaged:
    """Notifications branch on ``EnvelopeStatus`` through hand-listed tables, and answer a status
    they don't know about by staying quiet. These pin each table against the enum."""

    def test_every_status_is_either_notify_worthy_or_deliberately_silent(self):
        # A new member absent from both sides never notifies, and nothing else would say so.
        assert NOTIFY_WORTHY_STATUSES | {EnvelopeStatus.ALL_CLEAR} == set(EnvelopeStatus)

    def test_every_status_has_a_severity_rank(self):
        assert set(EnvelopeStatus.worst_first()) == set(EnvelopeStatus)

    def test_every_status_has_a_tone(self):
        assert set(STATUS_TONES) == set(EnvelopeStatus)

    def test_only_all_clear_ever_renders_green(self):
        # "success" is the pill that tells a reader there is nothing to do; only a clean run earns it.
        assert {s for s in EnvelopeStatus if envelope_tone(s) == "success"} == {EnvelopeStatus.ALL_CLEAR}

    def test_an_untoned_status_reads_as_a_failure_and_says_so(self, caplog):
        with caplog.at_level(logging.ERROR, logger="daiv.notifications"):
            assert envelope_tone("brand-new") == "failure"
        assert "untoned envelope status" in caplog.text


@pytest.mark.django_db
class TestNotifies:
    def _run(self, trigger_type, *, ci_failure=None):
        session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="x/y")
        run = Run.objects.create(session=session, trigger_type=trigger_type, repo_id="x/y", status=RunStatus.SUCCESSFUL)
        if ci_failure is not None:
            make_event(session, EventKind.CI_FAILED, run=run, state=EventState.HANDLED, wake=ci_failure == "waking")
        return run

    @pytest.mark.parametrize("origin", sorted(SessionOrigin.webhooks()))
    def test_a_webhook_question_is_left_to_the_forge_mention(self, origin):
        assert notifies(self._run(origin), EnvelopeStatus.NEEDS_INPUT) is False

    def test_a_prompt_driven_question_notifies(self):
        for origin in SessionOrigin.prompt_driven():
            assert notifies(SimpleNamespace(trigger_type=origin), EnvelopeStatus.NEEDS_INPUT) is True

    def test_other_webhook_outcomes_follow_notify_worthy(self):
        run = self._run(SessionOrigin.ISSUE_WEBHOOK)
        assert notifies(run, EnvelopeStatus.FAILED) is True
        assert notifies(run, EnvelopeStatus.ALL_CLEAR) is False

    @pytest.mark.parametrize("status", list(EnvelopeStatus))
    @pytest.mark.parametrize("origin", [SessionOrigin.PIPELINE_WEBHOOK, SessionOrigin.MR_WEBHOOK])
    def test_a_run_a_ci_failure_woke_notifies_whatever_its_outcome(self, origin, status):
        assert notifies(self._run(origin, ci_failure="waking"), status) is True

    def test_a_ci_failure_the_run_only_remembered_does_not(self):
        assert notifies(self._run(SessionOrigin.MR_WEBHOOK, ci_failure="remembered"), EnvelopeStatus.ALL_CLEAR) is False


def test_the_session_keys_fit_the_source_id_column():
    """SQLite does not enforce varchar length, so a key longer than the column only fails on Postgres."""
    session = SimpleNamespace(thread_id="0" * 36)
    keys = [
        notification_source_for_watch(session, "a" * 40)[1],
        notification_source_for_inbox_pause(session, "1" * 36)[1],
    ]
    assert max(map(len, keys)) <= Notification._meta.get_field("source_id").max_length
