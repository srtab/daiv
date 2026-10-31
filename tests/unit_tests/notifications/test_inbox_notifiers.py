from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

import pytest
from notifications.choices import EventType
from notifications.models import Notification
from notifications.policy import SOURCE_SESSION
from sessions.inbox.policy import report_run_limit
from sessions.models import EventKind, QuietReason, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import make_event

pytestmark = pytest.mark.django_db(transaction=True)


def _session(user) -> Session:
    return Session.objects.create(
        thread_id="t-paused", origin=SessionOrigin.CHAT, repo_id="group/repo", title="Fix CI", user=user
    )


def _block(session: Session, *, minutes_ago: int):
    return make_event(
        session,
        EventKind.CI_FAILED,
        wake=False,
        reason=QuietReason.RUN_LIMIT,
        created_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


def test_the_owner_is_told_once_per_streak(member_user):
    session = _session(member_user)
    first = _block(session, minutes_ago=10)

    report_run_limit(session)
    _block(session, minutes_ago=5)
    report_run_limit(session)

    notification = Notification.objects.get()
    assert notification.recipient == member_user
    assert (notification.source_type, notification.source_id) == (SOURCE_SESSION, f"t-paused:{first.pk}")
    assert notification.event_type == EventType.INBOX_PAUSED
    assert notification.link_url == reverse("session_detail", kwargs={"thread_id": "t-paused"})
    assert notification.context["runs_in_a_row"] == 3
    assert "3 times in a row" in notification.body


def test_a_person_starting_a_run_begins_a_new_streak(member_user):
    session = _session(member_user)
    _block(session, minutes_ago=30)
    report_run_limit(session)
    Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.CHAT,
        repo_id="group/repo",
        status=RunStatus.SUCCESSFUL,
        created_at=timezone.now() - timedelta(minutes=20),
    )
    _block(session, minutes_ago=5)

    report_run_limit(session)

    assert Notification.objects.count() == 2


def test_a_session_without_an_owner_tells_no_one():
    session = _session(None)
    _block(session, minutes_ago=1)

    report_run_limit(session)

    assert Notification.objects.count() == 0
