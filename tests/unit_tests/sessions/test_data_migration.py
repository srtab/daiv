import uuid
from datetime import timedelta

from django.apps import apps as global_apps
from django.utils import timezone

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from sessions.backfill import run_backfill
from sessions.models import Run, Session, SessionOrigin

from chat.models import ChatThread

pytestmark = pytest.mark.django_db


def test_activities_collapse_into_one_session_per_thread():
    tid = str(uuid.uuid4())
    a1 = Activity.objects.create(
        trigger_type=TriggerType.ISSUE_WEBHOOK,
        repo_id="g/r",
        thread_id=tid,
        status=ActivityStatus.SUCCESSFUL,
        external_username="alice",
    )
    a2 = Activity.objects.create(
        trigger_type=TriggerType.API_JOB, repo_id="g/r", thread_id=tid, status=ActivityStatus.FAILED, title="second run"
    )
    run_backfill(global_apps)
    session = Session.objects.get(pk=tid)
    assert session.origin == SessionOrigin.ISSUE_WEBHOOK  # earliest activity wins
    assert session.title == "second run"  # latest non-empty title wins
    assert set(Run.objects.filter(session=session).values_list("id", flat=True)) == {a1.id, a2.id}


def test_null_thread_activity_gets_minted_session():
    a = Activity.objects.create(trigger_type=TriggerType.UI_JOB, repo_id="g/r", thread_id=None)
    run_backfill(global_apps)
    run = Run.objects.get(pk=a.id)
    assert run.session_id  # minted
    assert Session.objects.filter(pk=run.session_id).exists()


def test_chat_thread_merges_into_existing_session(django_user_model):
    user = django_user_model.objects.create_user(username="u", email="u@x.io", password="x")  # noqa: S106
    tid = str(uuid.uuid4())
    Activity.objects.create(trigger_type=TriggerType.API_JOB, repo_id="g/r", thread_id=tid, title="activity title")
    ChatThread.objects.create(thread_id=tid, user=user, repo_id="g/r", title="chat title")
    run_backfill(global_apps)
    session = Session.objects.get(pk=tid)
    assert session.title == "chat title"  # chat metadata wins on merge
    assert session.user == user
    assert session.origin == SessionOrigin.API_JOB  # origin stays from the activity


def test_chat_only_thread_becomes_chat_session_with_zero_runs(django_user_model):
    user = django_user_model.objects.create_user(username="u", email="u@x.io", password="x")  # noqa: S106
    tid = str(uuid.uuid4())
    ChatThread.objects.create(thread_id=tid, user=user, repo_id="g/r", ref="main", title="hello")
    run_backfill(global_apps)
    session = Session.objects.get(pk=tid)
    assert session.origin == SessionOrigin.CHAT
    assert session.user == user
    assert session.runs.count() == 0


def test_created_at_and_timestamps_preserved():
    tid = str(uuid.uuid4())
    old = timezone.now() - timedelta(days=30)
    a = Activity.objects.create(trigger_type=TriggerType.API_JOB, repo_id="g/r", thread_id=tid)
    Activity.objects.filter(pk=a.pk).update(created_at=old)  # bypass auto_now_add
    run_backfill(global_apps)
    assert Run.objects.get(pk=a.pk).created_at == old
    assert Session.objects.get(pk=tid).created_at == old


def test_backfill_is_idempotent():
    tid = str(uuid.uuid4())
    Activity.objects.create(trigger_type=TriggerType.API_JOB, repo_id="g/r", thread_id=tid)
    run_backfill(global_apps)
    run_backfill(global_apps)
    assert Session.objects.filter(pk=tid).count() == 1
    assert Run.objects.count() == 1
