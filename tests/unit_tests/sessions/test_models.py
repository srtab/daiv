import uuid

from django.db import IntegrityError, transaction

import pytest
from sessions.executor.result import parse_agent_result
from sessions.models import Run, RunStatus, Session, SessionOrigin, SessionRepository, agent_result_field_updates

from automation.agent.workspace.session import SandboxAcquisition

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
    assert RunStatus.terminal() == frozenset({RunStatus.SUCCESSFUL, RunStatus.WAITING_INPUT, RunStatus.FAILED})


@pytest.mark.django_db
def test_a_successful_task_result_with_a_question_syncs_to_waiting_input(create_db_task_result):
    question = {"questions": [{"header": "DB", "question": "Which?", "options": [], "multi_select": False}]}
    task_result = create_db_task_result(return_value={"response": "**DB**: Which?", "question": question})
    run = _mk_run(_mk_session(), status=RunStatus.RUNNING, task_result=task_result)

    run.sync_and_save()
    run.refresh_from_db()

    assert run.status == RunStatus.WAITING_INPUT
    assert run.result_summary == "**DB**: Which?"
    assert run.question == question


@pytest.mark.django_db
def test_resyncing_a_waiting_run_changes_nothing(create_db_task_result):
    task_result = create_db_task_result(return_value={"response": "q", "question": {"questions": []}})
    run = _mk_run(_mk_session(), status=RunStatus.RUNNING, task_result=task_result)
    run.sync_and_save()

    assert run.sync_from_task_result() == []


@pytest.mark.django_db
def test_a_successful_task_result_without_a_question_stays_successful(create_db_task_result):
    task_result = create_db_task_result(return_value={"response": "done"})
    run = _mk_run(_mk_session(), status=RunStatus.RUNNING, task_result=task_result)
    run.sync_and_save()

    assert run.status == RunStatus.SUCCESSFUL
    assert run.question is None


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
        "pipeline_webhook",
        "delegated_job",
        "delegation_resume",
    }


def test_classified_origins_exclude_chat_and_pipeline_webhook():
    """Classification is "universal for non-chat" except pipeline-watch runs, which have a
    separate result path. Pinning the set forces a newly-added SessionOrigin to decide."""
    assert SessionOrigin.classified() == frozenset(SessionOrigin.values) - {
        SessionOrigin.CHAT,
        SessionOrigin.PIPELINE_WEBHOOK,
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
        SessionOrigin.DELEGATION_RESUME,
    }


def test_sandbox_acquisition_column_fits_every_member():
    """The column has no ``choices``, and the measurement write swallows a ``DataError``, so an overlong member
    would silently drop every run's measurements."""
    limit = Run._meta.get_field("sandbox_acquisition").max_length
    assert max(len(member.value) for member in SandboxAcquisition) <= limit


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


def test_run_status_superset_of_task_result_status():
    """``Run.sync_from_task_result`` assigns ``DBTaskResult.status`` straight into
    ``Run.status``, so every django-tasks status must be a valid ``RunStatus`` member —
    otherwise a synced row would violate the ``run_status_valid`` CHECK constraint. This
    guards a django-tasks upgrade that introduces a new status value."""
    from django.tasks import TaskResultStatus

    assert set(TaskResultStatus.values) <= set(RunStatus.values)


# --- RunManager.visible_to ownership (run-level authorization boundary) -----


def test_run_visible_to_admin_sees_all(admin_user, django_user_model):
    other = django_user_model.objects.create_user(username="ro", email="ro@x.io", password="x")  # noqa: S106
    session = _mk_session(user=other)
    mine = _mk_run(session, user=admin_user, status=RunStatus.QUEUED)
    theirs = _mk_run(session, user=other, status=RunStatus.SUCCESSFUL)
    visible = set(Run.objects.visible_to(admin_user).values_list("pk", flat=True))
    assert {mine.pk, theirs.pk} <= visible


def test_run_visible_to_matches_user_and_external_username_but_not_stranger(django_user_model):
    user = django_user_model.objects.create_user(username="ru1", email="ru1@x.io", password="x")  # noqa: S106
    other = django_user_model.objects.create_user(username="ru2", email="ru2@x.io", password="x")  # noqa: S106
    # Webhook session: runs are exempt from the one-active-per-session constraint, so
    # several active runs can coexist for this authorization test.
    session = _mk_session(user=None, origin=SessionOrigin.ISSUE_WEBHOOK)
    own = _mk_run(session, user=user, trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    ext = _mk_run(session, user=None, external_username="ru1", trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    stranger = _mk_run(session, user=other, trigger_type=SessionOrigin.ISSUE_WEBHOOK)
    visible = set(Run.objects.visible_to(user).values_list("pk", flat=True))
    assert own.pk in visible
    assert ext.pk in visible
    assert stranger.pk not in visible


def test_run_visible_to_matches_subscribed_schedule(django_user_model):
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
    assert run.pk in set(Run.objects.visible_to(sub).values_list("pk", flat=True))


@pytest.mark.django_db
class TestEffectiveMuted:
    def _run(self, *, muted=None, schedule=None):
        origin = SessionOrigin.SCHEDULE if schedule else SessionOrigin.API_JOB
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=origin, repo_id="x/y", scheduled_job=schedule
        )
        return Run.objects.create(
            session=session, trigger_type=origin, repo_id="x/y", status=RunStatus.SUCCESSFUL, muted=muted
        )

    def test_run_override_wins(self, member_user):
        from schedules.models import Frequency, ScheduledJob

        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="n",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
            muted=True,
        )
        assert self._run(muted=False, schedule=schedule).effective_muted is False
        assert self._run(muted=True, schedule=schedule).effective_muted is True

    def test_inherits_schedule_when_no_override(self, member_user):
        from schedules.models import Frequency, ScheduledJob

        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="n",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
            muted=True,
        )
        assert self._run(muted=None, schedule=schedule).effective_muted is True

    def test_non_scheduled_defaults_false(self):
        assert self._run(muted=None, schedule=None).effective_muted is False
        assert self._run(muted=True, schedule=None).effective_muted is True


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


@pytest.mark.django_db
def test_session_mcp_overrides_defaults_to_empty_dict():
    s = Session.objects.create(thread_id="t-mcp", origin=SessionOrigin.UI_JOB, repo_id="g/r")
    assert s.mcp_overrides == {}


def test_run_defaults_to_classify_eligible():
    """New runs are classify-eligible by default so the reclassify backstop stays a catch-all."""
    run = _mk_run(_mk_session(), status=RunStatus.SUCCESSFUL)
    assert run.classify_eligible is True


def _mk_artifact(run: Run, **kwargs):
    from sessions.models import RunArtifact

    defaults = {"title": "Report", "filename": "report.md", "content_type": "text/markdown", "size": 3}
    defaults.update(kwargs)
    return RunArtifact(run=run, **defaults)


def test_artifact_upload_to_is_unique_per_row_and_keeps_extension():
    from sessions.models import artifact_upload_to

    run = _mk_run(_mk_session())
    artifact = _mk_artifact(run)
    assert artifact_upload_to(artifact, "/workspace/tmp/Report.MD") == f"artifacts/{run.pk}/{artifact.pk}.md"
    assert artifact_upload_to(artifact, "noext") == f"artifacts/{run.pk}/{artifact.pk}"
    assert artifact_upload_to(artifact, "report.md", revision="r1") == f"artifacts/{run.pk}/{artifact.pk}-r1.md"


def test_artifact_kind_follows_content_type():
    from sessions.artifacts import ArtifactKind

    run = _mk_run(_mk_session())
    assert _mk_artifact(run, content_type="text/html").kind == ArtifactKind.HTML
    assert _mk_artifact(run, content_type="application/pdf").kind == ArtifactKind.OTHER


def test_artifact_urls_are_scoped_to_the_session():
    run = _mk_run(_mk_session())
    artifact = _mk_artifact(run)
    artifact.save()
    base = f"/dashboard/sessions/{run.session_id}/artifacts/{artifact.pk}/"
    assert artifact.get_absolute_url() == base
    assert artifact.get_raw_url() == f"{base}raw/"
    assert artifact.get_download_url() == f"{base}raw/?download=1"
    assert str(artifact) == f"report.md (text/markdown) for run {run.pk}"


def test_artifact_visible_to_follows_session_visibility(admin_user, member_user, other_user):
    from sessions.models import RunArtifact

    mine = _mk_artifact(_mk_run(_mk_session(user=member_user), user=member_user))
    mine.save()
    on_my_session_by_other = _mk_artifact(_mk_run(_mk_session(user=member_user), user=other_user))
    on_my_session_by_other.save()
    theirs = _mk_artifact(_mk_run(_mk_session(user=other_user), user=other_user))
    theirs.save()

    assert set(RunArtifact.objects.visible_to(member_user)) == {mine, on_my_session_by_other}
    assert set(RunArtifact.objects.visible_to(admin_user)) == {mine, on_my_session_by_other, theirs}


@pytest.mark.parametrize(("code_changes", "expected"), [(True, {"code_changes": True}), (False, {})])
def test_agent_result_field_updates_maps_code_changes_only_when_set(code_changes, expected):
    """False is left out so a later empty result never clears a flag an earlier sync recorded."""
    assert agent_result_field_updates(parse_agent_result({"code_changes": code_changes})) == expected


def test_session_repository_unique_per_session():
    session = _mk_session()
    SessionRepository.objects.create(session=session, position=1, repo_id="g/mod")

    with pytest.raises(IntegrityError), transaction.atomic():
        SessionRepository.objects.create(session=session, position=2, repo_id="g/mod")
    with pytest.raises(IntegrityError), transaction.atomic():
        SessionRepository.objects.create(session=session, position=1, repo_id="g/lib")

    SessionRepository.objects.create(session=_mk_session(), position=1, repo_id="g/mod")


def test_merge_request_links_prefers_list_and_falls_back_to_legacy_url():
    run = _mk_run(
        _mk_session(),
        merge_request_web_url="https://git/g/repo/-/merge_requests/1",
        merge_requests=[
            {"repo_id": "group/repo", "merge_request_id": 1, "web_url": "https://git/g/repo/-/merge_requests/1"},
            {"repo_id": "g/mod", "merge_request_id": 7, "web_url": "https://git/g/mod/-/merge_requests/7"},
        ],
    )
    assert run.merge_request_links() == [
        {"repo_id": "group/repo", "url": "https://git/g/repo/-/merge_requests/1"},
        {"repo_id": "g/mod", "url": "https://git/g/mod/-/merge_requests/7"},
    ]

    run.merge_requests = []
    assert run.merge_request_links() == [{"repo_id": "group/repo", "url": "https://git/g/repo/-/merge_requests/1"}]

    run.merge_request_web_url = ""
    assert run.merge_request_links() == []


def test_primary_merge_request_url_prefers_the_main_repositorys():
    sibling = {"repo_id": "g/mod", "merge_request_id": 7, "web_url": "https://git/g/mod/-/merge_requests/7"}
    run = _mk_run(
        _mk_session(), merge_request_web_url="https://git/g/repo/-/merge_requests/1", merge_requests=[sibling]
    )
    assert run.primary_merge_request_url() == "https://git/g/repo/-/merge_requests/1"

    run.merge_request_web_url = ""
    assert run.primary_merge_request_url() == "https://git/g/mod/-/merge_requests/7"

    run.merge_requests = []
    assert run.primary_merge_request_url() is None


def test_field_updates_include_merge_requests_and_errors():
    result = parse_agent_result({
        "response": "r",
        "merge_requests": [
            {"repo_id": "g/mod", "merge_request_id": 7, "web_url": "https://git/g/mod/-/merge_requests/7"}
        ],
        "publish_errors": [{"repo_id": "g/lib", "error": "boom"}],
    })

    updates = agent_result_field_updates(result)

    assert updates["merge_requests"] == [
        {"repo_id": "g/mod", "merge_request_id": 7, "web_url": "https://git/g/mod/-/merge_requests/7"}
    ]
    assert updates["publish_errors"] == [{"repo_id": "g/lib", "error": "boom"}]


def test_field_updates_leave_out_empty_lists():
    assert agent_result_field_updates(parse_agent_result({"response": "r"})) == {}
