"""Sessions, runs, schedules and spend records shaped the way the dashboard reads them."""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from sessions.models import Run, RunStatus, Session, SessionOrigin, SpendRecord

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
    moment = timezone.now() - timedelta(minutes=minutes_ago)
    return Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=origin,
        repo_id="daiv/test",
        user=user,
        title=title,
        scheduled_job=scheduled_job,
        created_at=moment,
        last_active_at=moment,
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


def spend_session(user=None, repo_id="acme/api", origin=SessionOrigin.CHAT, title="A session") -> Session:
    return Session.objects.create(thread_id=str(uuid.uuid4()), origin=origin, repo_id=repo_id, user=user, title=title)


def spend_run(session, *, status=RunStatus.SUCCESSFUL, days_ago=0, code_changes=False, trigger=None) -> Run:
    run = Run.objects.create(
        session=session,
        trigger_type=trigger or session.origin,
        repo_id=session.repo_id,
        user=session.user,
        status=status,
        code_changes=code_changes,
    )
    Run.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    return run


def spend_record(run=None, *, days_ago=0, cost="1", model="claude-sonnet-4-6", provider="anthropic", **fields):
    if run is not None:
        fields = {
            "run": run,
            "session": run.session,
            "repo_id": run.repo_id,
            "trigger_type": run.trigger_type,
            "user": run.user,
            **fields,
        }
    fields.setdefault("input_tokens", 100)
    fields.setdefault("output_tokens", 10)
    fields.setdefault("total_tokens", fields["input_tokens"] + fields["output_tokens"])
    return SpendRecord.objects.create(
        occurred_at=timezone.now() - timedelta(days=days_ago),
        model=model,
        provider_slug=provider,
        provider_name=provider.title(),
        cost_usd=Decimal(cost),
        **fields,
    )
