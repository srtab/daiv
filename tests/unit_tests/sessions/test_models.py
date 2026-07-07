import uuid

from django.db import IntegrityError

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

pytestmark = pytest.mark.django_db


def _mk_session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.API_JOB, "repo_id": "group/repo"}
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _mk_run(session: Session, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.API_JOB, "repo_id": session.repo_id, "status": RunStatus.READY}
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


def test_run_status_terminal_set():
    assert RunStatus.terminal() == frozenset({RunStatus.SUCCESSFUL, RunStatus.FAILED})


def test_session_origin_includes_chat():
    assert SessionOrigin.CHAT == "chat"
    # All Activity trigger values survive with identical strings.
    assert {c[0] for c in SessionOrigin.choices} == {
        "chat",
        "api_job",
        "mcp_job",
        "schedule",
        "ui_job",
        "issue_webhook",
        "mr_webhook",
    }


def test_one_active_api_run_per_session_constraint():
    session = _mk_session()
    _mk_run(session, status=RunStatus.READY, trigger_type=SessionOrigin.API_JOB)
    with pytest.raises(IntegrityError):
        _mk_run(session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.API_JOB)


def test_webhook_runs_exempt_from_active_constraint():
    session = _mk_session(origin=SessionOrigin.ISSUE_WEBHOOK)
    _mk_run(session, status=RunStatus.READY, trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    # Second active webhook run on the same session is allowed (FIFO handled by QUEUED).
    _mk_run(session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.ISSUE_WEBHOOK)


def test_queued_runs_stack_freely():
    session = _mk_session()
    _mk_run(session, status=RunStatus.READY)
    _mk_run(session, status=RunStatus.QUEUED)
    _mk_run(session, status=RunStatus.QUEUED)


def test_active_run_id_nonempty_constraint():
    with pytest.raises(IntegrityError):
        _mk_session(active_run_id="")


def test_by_owner_admin_sees_all(admin_user, django_user_model):
    other = django_user_model.objects.create_user(username="other", email="o@x.io", password="x")  # noqa: S106
    _mk_session(user=other)
    assert Session.objects.by_owner(admin_user).count() == 1


def test_by_owner_matches_session_user(django_user_model):
    user = django_user_model.objects.create_user(username="u1", email="u1@x.io", password="x")  # noqa: S106
    other = django_user_model.objects.create_user(username="u2", email="u2@x.io", password="x")  # noqa: S106
    mine = _mk_session(user=user)
    _mk_session(user=other)
    assert list(Session.objects.by_owner(user)) == [mine]


def test_by_owner_matches_run_actor(django_user_model):
    """A session owned by nobody is visible to a user who acted in one of its runs."""
    user = django_user_model.objects.create_user(username="gituser", email="g@x.io", password="x")  # noqa: S106
    session = _mk_session(user=None, origin=SessionOrigin.ISSUE_WEBHOOK)
    _mk_run(session, trigger_type=SessionOrigin.ISSUE_WEBHOOK, external_username="gituser")
    assert list(Session.objects.by_owner(user)) == [session]


def test_with_latest_status_annotation():
    session = _mk_session()
    _mk_run(session, status=RunStatus.SUCCESSFUL)
    _mk_run(session, status=RunStatus.RUNNING)
    annotated = Session.objects.with_latest_status().get(pk=session.pk)
    assert annotated.latest_run_status == RunStatus.RUNNING


def test_run_is_retryable_mirrors_activity_semantics():
    session = _mk_session()
    done = _mk_run(session, status=RunStatus.SUCCESSFUL)
    webhook = _mk_run(session, status=RunStatus.FAILED, trigger_type=SessionOrigin.MR_WEBHOOK)
    running = _mk_run(session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.UI_JOB)
    assert done.is_retryable is True
    assert webhook.is_retryable is False
    assert running.is_retryable is False


def test_run_duration():
    from datetime import timedelta

    from django.utils import timezone

    session = _mk_session()
    now = timezone.now()
    run = _mk_run(session, started_at=now, finished_at=now + timedelta(seconds=90))
    assert run.duration == 90.0
