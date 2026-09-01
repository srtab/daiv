import logging
from datetime import timedelta

from django.utils import timezone

from notifications.policy import (
    envelope_tone,
    notify_worthy,
    notify_worthy_statuses,
    status_severity,
    status_tones,
    within_relevance_window,
)
from sessions.models import EnvelopeStatus
from sessions.tasks import RECLASSIFY_MAX_AGE


def test_notify_worthy_truth_table():
    assert notify_worthy(EnvelopeStatus.FOUND_ISSUES) is True
    assert notify_worthy(EnvelopeStatus.NEEDS_ATTENTION) is True
    assert notify_worthy(EnvelopeStatus.FAILED) is True
    assert notify_worthy(EnvelopeStatus.ALL_CLEAR) is False


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
        assert notify_worthy_statuses() | {EnvelopeStatus.ALL_CLEAR} == set(EnvelopeStatus)

    def test_every_status_has_a_severity_rank(self):
        assert set(EnvelopeStatus.worst_first()) == set(EnvelopeStatus)

    def test_every_status_has_a_tone(self):
        assert set(status_tones()) == set(EnvelopeStatus)

    def test_only_all_clear_ever_renders_green(self):
        # "success" is the pill that tells a reader there is nothing to do; only a clean run earns it.
        assert {s for s in EnvelopeStatus if envelope_tone(s) == "success"} == {EnvelopeStatus.ALL_CLEAR}

    def test_an_untoned_status_reads_as_a_failure_and_says_so(self, caplog):
        with caplog.at_level(logging.ERROR, logger="daiv.notifications"):
            assert envelope_tone("brand-new") == "failure"
        assert "untoned envelope status" in caplog.text
