"""Sessions, runs and schedules shaped the way the sidebar lists read them."""

import uuid
from datetime import timedelta

from django.utils import timezone

from sessions.models import Run, Session, SessionOrigin

from schedules.models import Frequency, ScheduledJob


def make_schedule(user, **fields) -> ScheduledJob:
    defaults = {
        "name": "Nightly audit",
        "prompt": "p",
        "repos": [{"repo_id": "daiv/test", "ref": ""}],
        "frequency": Frequency.DAILY,
        "time": "09:00",
    }
    return ScheduledJob.objects.create(user=user, **(defaults | fields))


def make_session(user, *, minutes_ago=0, title="A session", scheduled_job=None, origin=SessionOrigin.CHAT) -> Session:
    return Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=origin,
        repo_id="daiv/test",
        user=user,
        title=title,
        scheduled_job=scheduled_job,
        last_active_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


def make_run(session, status, *, minutes_ago=0, trigger_type=None, batch_id=None) -> Run:
    run = Run.objects.create(
        session=session,
        status=status,
        trigger_type=trigger_type or session.origin,
        repo_id=session.repo_id,
        user=session.user,
        batch_id=batch_id,
    )
    Run.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
    return run


def make_firing(schedule, *statuses, minutes_ago=0):
    """One dispatch of ``schedule``: a session per status, each with a schedule run of one shared batch."""
    batch_id = uuid.uuid4()
    sessions = []
    for status in statuses:
        session = make_session(
            schedule.user, minutes_ago=minutes_ago, scheduled_job=schedule, origin=SessionOrigin.SCHEDULE
        )
        make_run(session, status, minutes_ago=minutes_ago, batch_id=batch_id)
        sessions.append(session)
    return batch_id, sessions
