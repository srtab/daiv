from datetime import timedelta

from django.utils import timezone

from notifications.policy import notify_worthy, within_relevance_window
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
