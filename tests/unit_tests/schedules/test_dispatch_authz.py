"""Cron dispatch + run-now behavior when the schedule owner lost repository access."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sessions.models import Run

from codebase.authorization import RepositoryAccessDenied
from schedules.models import Frequency, ScheduledJob
from schedules.tasks import dispatch_scheduled_jobs_cron_task


@pytest.fixture
def due_schedule(member_user):
    schedule = ScheduledJob(
        user=member_user,
        name="s",
        prompt="p",
        repos=[{"repo_id": "a/b", "ref": ""}],
        frequency=Frequency.HOURLY,
        is_enabled=True,
    )
    schedule.full_clean()
    schedule.compute_next_run()
    schedule.next_run_at = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule.save()
    return schedule


@pytest.mark.django_db(transaction=True)
def test_dispatch_denied_owner_advances_without_runs(due_schedule, caplog):
    # ``daiv.schedules`` is the logger name used by daiv/schedules/tasks.py.
    caplog.set_level(logging.WARNING, logger="daiv.schedules")
    with patch("sessions.services.aassert_can_run", new=AsyncMock(side_effect=RepositoryAccessDenied(["a/b"]))):
        dispatch_scheduled_jobs_cron_task.func()

    due_schedule.refresh_from_db()
    assert Run.objects.count() == 0
    assert due_schedule.next_run_at > datetime.now(tz=UTC)  # advanced, not hot-looping
    assert due_schedule.is_enabled  # not disabled — access may come back

    # A denial must log a WARNING (no traceback) for this schedule, not logger.exception.
    # Guards against reverting the isinstance(err, RepositoryAccessDenied) branch in tasks.py:
    # the recovery outcome above is identical for any Exception, so only the log level differs.
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("skipped" in record.getMessage() and str(due_schedule.pk) in record.getMessage() for record in warnings)
    # No ERROR-level record and no traceback (exc_info) was emitted on the denial path.
    assert not any(record.levelno >= logging.ERROR or record.exc_info for record in caplog.records)
