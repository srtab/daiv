from datetime import timedelta

from django.utils import timezone

import pytest
from notifications.signals import _within_relevance_window, notify_worthy
from sessions.models import EnvelopeStatus
from sessions.tasks import RECLASSIFY_MAX_AGE


def test_notify_worthy_truth_table():
    assert notify_worthy(EnvelopeStatus.FOUND_ISSUES) is True
    assert notify_worthy(EnvelopeStatus.NEEDS_ATTENTION) is True
    assert notify_worthy(EnvelopeStatus.FAILED) is True
    assert notify_worthy(EnvelopeStatus.ALL_CLEAR) is False


def test_window_rejects_none_and_too_old():
    assert _within_relevance_window(None) is False
    assert _within_relevance_window(timezone.now() - RECLASSIFY_MAX_AGE - timedelta(hours=1)) is False


def test_window_accepts_recent():
    assert _within_relevance_window(timezone.now() - timedelta(minutes=5)) is True


@pytest.mark.django_db
def test_window_respects_not_before(settings_not_before):
    # finished before the cutoff is suppressed; after it (and recent) is allowed.
    cutoff = settings_not_before
    assert _within_relevance_window(cutoff - timedelta(minutes=1)) is False
    assert _within_relevance_window(cutoff + timedelta(minutes=1)) is True


@pytest.fixture
def settings_not_before(monkeypatch):
    from notifications.conf import settings as notif_settings

    cutoff = timezone.now() - timedelta(hours=1)
    monkeypatch.setattr(notif_settings, "NOTIFY_NOT_BEFORE", cutoff)
    return cutoff
