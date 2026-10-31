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


open_watches_migration = import_module("sessions.migrations.0023_migrate_open_watches")


@pytest.mark.django_db
def test_open_watches_become_ownerships_of_their_merge_request_thread(member_user):
    from django.apps import apps as global_apps

    from sessions.models import MergeRequestOwnership

    def _watch(state, iid):
        return Session.objects.create(
            thread_id=str(uuid.uuid4()),
            origin=SessionOrigin.PIPELINE_WEBHOOK,
            repo_id="group/repo",
            ref=f"daiv/branch-{iid}",
            merge_request_iid=iid,
            user=member_user,
            watch_state=state,
            watch_attempts=2,
            watch_pipeline_id=830724,
        )

    watching, fixing = _watch("watching", 1), _watch("fixing", 2)
    for state, iid in (("green", 3), ("exhausted", 4), ("off", 5)):
        _watch(state, iid)

    open_watches_migration.migrate_open_watches(global_apps, None)

    rows = {row.merge_request_iid: row for row in MergeRequestOwnership.objects.all()}
    assert set(rows) == {1, 2}
    for session in (watching, fixing):
        row = rows[session.merge_request_iid]
        assert (row.session_id, row.owner_id, row.source_branch) == (session.pk, member_user.pk, session.ref)
        assert (row.attempts, row.last_pipeline_id, row.last_daiv_sha) == (2, 830724, "")
