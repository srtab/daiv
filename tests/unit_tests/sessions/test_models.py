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
        "delegated_job",
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
    mine = _mk_session(user=admin_user)
    theirs = _mk_session(user=other)
    orphan = _mk_session(user=None, external_username="ext")
    # Admin sees all sessions, including ones it does not own. Subset (not equality)
    # keeps the assertion correct even if other tests committed rows into the shared
    # in-memory DB (async writes can escape the transaction rollback).
    visible = set(Session.objects.by_owner(admin_user).values_list("pk", flat=True))
    assert {mine.pk, theirs.pk, orphan.pk} <= visible


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


# --- Enum / constraint drift guards ----------------------------------------


def test_active_constraint_literals_match_enums():
    """The partial unique constraint hardcodes literals (Django serializes them into
    migrations). Guard against the enum drifting away from those literals, which would
    silently break the one-active-run-per-session guarantee."""
    constraint = next(c for c in Run._meta.constraints if c.name == "run_one_active_per_session")
    conditions = dict(constraint.condition.children)
    assert set(conditions["status__in"]) == {RunStatus.READY, RunStatus.RUNNING}
    assert set(conditions["trigger_type__in"]) == {
        SessionOrigin.API_JOB,
        SessionOrigin.MCP_JOB,
        SessionOrigin.DELEGATED_JOB,
    }


def test_session_origin_check_constraint_rejects_unknown_value():
    with pytest.raises(IntegrityError):
        _mk_session(origin="bogus_origin")


def test_run_status_check_constraint_rejects_unknown_value():
    session = _mk_session()
    with pytest.raises(IntegrityError):
        _mk_run(session, status="BOGUS")


def test_run_trigger_type_check_constraint_rejects_unknown_value():
    session = _mk_session()
    with pytest.raises(IntegrityError):
        _mk_run(session, trigger_type="bogus_trigger")


def test_run_agent_thinking_level_check_constraint():
    """Blank ("" = no override) and valid levels pass; an unknown level is rejected."""
    from core.models import ThinkingLevelChoices

    session = _mk_session()
    _mk_run(session, agent_thinking_level="")  # no override
    _mk_run(session, status=RunStatus.QUEUED, agent_thinking_level=ThinkingLevelChoices.HIGH)
    with pytest.raises(IntegrityError):
        _mk_run(session, status=RunStatus.QUEUED, agent_thinking_level="ludicrous")


def test_session_agent_thinking_level_check_constraint_rejects_unknown_value():
    with pytest.raises(IntegrityError):
        _mk_session(agent_thinking_level="ludicrous")


def test_run_notify_on_check_constraint():
    """NULL ("no override") and valid NotifyOn values pass; an unknown value is rejected."""
    from notifications.choices import NotifyOn

    session = _mk_session()
    _mk_run(session, notify_on=None)  # no override
    _mk_run(session, status=RunStatus.QUEUED, notify_on=NotifyOn.ALWAYS)
    with pytest.raises(IntegrityError):
        _mk_run(session, status=RunStatus.QUEUED, notify_on="sometimes")


def test_run_status_superset_of_task_result_status():
    """``Run.sync_from_task_result`` assigns ``DBTaskResult.status`` straight into
    ``Run.status``, so every django-tasks status must be a valid ``RunStatus`` member —
    otherwise a synced row would violate the ``run_status_valid`` CHECK constraint. This
    guards a django-tasks upgrade that introduces a new status value."""
    from django_tasks.base import TaskResultStatus

    assert set(TaskResultStatus.values) <= set(RunStatus.values)


# --- RunManager.by_owner (run-level authorization boundary) ----------------


def test_run_by_owner_admin_sees_all(admin_user, django_user_model):
    other = django_user_model.objects.create_user(username="ro", email="ro@x.io", password="x")  # noqa: S106
    session = _mk_session(user=other)
    mine = _mk_run(session, user=admin_user, status=RunStatus.QUEUED)
    theirs = _mk_run(session, user=other, status=RunStatus.SUCCESSFUL)
    visible = set(Run.objects.by_owner(admin_user).values_list("pk", flat=True))
    assert {mine.pk, theirs.pk} <= visible


def test_run_by_owner_matches_user_and_external_username_but_not_stranger(django_user_model):
    user = django_user_model.objects.create_user(username="ru1", email="ru1@x.io", password="x")  # noqa: S106
    other = django_user_model.objects.create_user(username="ru2", email="ru2@x.io", password="x")  # noqa: S106
    # Webhook session: runs are exempt from the one-active-per-session constraint, so
    # several active runs can coexist for this authorization test.
    session = _mk_session(user=None, origin=SessionOrigin.ISSUE_WEBHOOK)
    own = _mk_run(session, user=user, trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    ext = _mk_run(session, user=None, external_username="ru1", trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    stranger = _mk_run(session, user=other, trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    visible = set(Run.objects.by_owner(user).values_list("pk", flat=True))
    assert own.pk in visible
    assert ext.pk in visible
    assert stranger.pk not in visible


def test_run_by_owner_matches_subscribed_schedule(django_user_model):
    from schedules.models import Frequency, ScheduledJob

    owner = django_user_model.objects.create_user(username="rowner", email="rowner@x.io", password="x")  # noqa: S106
    sub = django_user_model.objects.create_user(username="rsub", email="rsub@x.io", password="x")  # noqa: S106
    sched = ScheduledJob.objects.create(
        user=owner, name="s", prompt="p", repos=[{"repo_id": "x/y", "ref": ""}], frequency=Frequency.DAILY, time="12:00"
    )
    sched.subscribers.add(sub)
    session = _mk_session(user=owner, origin=SessionOrigin.SCHEDULE, scheduled_job=sched)
    run = _mk_run(session, user=owner, trigger_type=SessionOrigin.SCHEDULE, status=RunStatus.SUCCESSFUL)
    # A subscriber to the session's schedule can see its runs.
    assert run.pk in set(Run.objects.by_owner(sub).values_list("pk", flat=True))


# --- effective_notify_on ---------------------------------------------------


def test_effective_notify_on_explicit_override_wins(django_user_model):
    from notifications.choices import NotifyOn

    user = django_user_model.objects.create_user(username="n1", email="n1@x.io", password="x")  # noqa: S106
    session = _mk_session(user=user)
    run = _mk_run(session, user=user, notify_on=NotifyOn.ALWAYS)
    # Even though the user default would apply, the explicit per-run override takes precedence.
    assert run.effective_notify_on == NotifyOn.ALWAYS


def test_effective_notify_on_falls_back_to_never_without_override_schedule_or_user():
    from notifications.choices import NotifyOn

    session = _mk_session(user=None)
    run = _mk_run(session, user=None, notify_on=None)
    assert run.effective_notify_on == NotifyOn.NEVER


# --- delegate_jobs data model -------------------------------------------


def test_delegated_job_is_accepted_by_enum_check_constraints():
    session = _mk_session(origin=SessionOrigin.DELEGATED_JOB)
    _mk_run(session, trigger_type=SessionOrigin.DELEGATED_JOB, status=RunStatus.QUEUED)


def test_one_continuation_per_batch_enforced():
    batch = uuid.uuid4()
    session = _mk_session()
    _mk_run(session, status=RunStatus.QUEUED, continuation_of_batch_id=batch)
    with pytest.raises(IntegrityError):
        _mk_run(session, status=RunStatus.QUEUED, continuation_of_batch_id=batch)


def test_null_continuation_of_batch_not_deduplicated():
    """Multiple runs with NULL continuation_of_batch_id coexist (partial constraint)."""
    session = _mk_session()
    _mk_run(session, status=RunStatus.QUEUED)
    _mk_run(session, status=RunStatus.QUEUED)  # no IntegrityError


def test_spawn_depth_cannot_exceed_cap():
    """The delegation-depth fuse is enforced at the DB, not only in the delegate_jobs tool."""
    from sessions.models import MAX_SPAWN_DEPTH

    _mk_session(spawn_depth=MAX_SPAWN_DEPTH)  # at the cap is allowed
    with pytest.raises(IntegrityError):
        _mk_session(spawn_depth=MAX_SPAWN_DEPTH + 1)


# --- message_id ------------------------------------------------------------


def test_run_message_id_defaults_blank_and_persists(session_fixture):
    run = Run.objects.create(
        session=session_fixture,
        trigger_type=SessionOrigin.CHAT,
        repo_id=session_fixture.repo_id,
        status=RunStatus.SUCCESSFUL,
    )
    assert run.message_id == ""

    run.message_id = "h-42"
    run.save(update_fields=["message_id"])
    run.refresh_from_db()
    assert run.message_id == "h-42"
