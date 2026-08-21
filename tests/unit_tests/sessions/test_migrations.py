import uuid
from importlib import import_module

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

muted_migration = import_module("sessions.migrations.0006_run_muted_classify_eligible")


def _run(status):
    """A run in its own session so the one-active-per-session constraint never fires."""
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="x/y")
    return Run.objects.create(session=session, trigger_type=SessionOrigin.API_JOB, repo_id="x/y", status=status)


@pytest.mark.django_db
def test_backfill_ineligible_freezes_only_terminal_runs():
    """The pre-deploy freeze must catch terminal rows (SUCCESSFUL/FAILED) and leave a run that is still
    RUNNING/QUEUED across the deploy eligible, so it is still recoverable when it finishes."""
    from django.apps import apps as global_apps

    runs = {
        status: _run(status) for status in (RunStatus.SUCCESSFUL, RunStatus.FAILED, RunStatus.RUNNING, RunStatus.QUEUED)
    }
    assert all(r.classify_eligible for r in runs.values())  # AddField default is True

    muted_migration.backfill_ineligible(global_apps, None)

    for r in runs.values():
        r.refresh_from_db()
    assert runs[RunStatus.SUCCESSFUL].classify_eligible is False
    assert runs[RunStatus.FAILED].classify_eligible is False
    assert runs[RunStatus.RUNNING].classify_eligible is True
    assert runs[RunStatus.QUEUED].classify_eligible is True
