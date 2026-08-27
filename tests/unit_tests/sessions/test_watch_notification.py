import pytest
from notifications.choices import EventType
from notifications.models import Notification
from notifications.policy import SOURCE_SESSION
from sessions.models import Session, SessionOrigin

from codebase.base import Job

from .conftest import make_pipeline


@pytest.mark.django_db(transaction=True)
async def test_it_hands_the_pipeline_facts_to_the_notifications_app(monkeypatch, django_user_model):
    from sessions.pipeline_watch import anotify_watch_exhausted

    user = await django_user_model.objects.acreate(username="owner", email="owner@example.com")
    session = await Session.objects.acreate(
        thread_id="t", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", merge_request_iid=7, user=user
    )

    calls = []
    monkeypatch.setattr("notifications.watch_notifiers.emit_watch_exhausted", lambda **kw: calls.append(kw))

    pipeline = make_pipeline(
        jobs=[
            Job(id=1, name="tests", status="failed", stage="test", allow_failure=False),
            Job(id=2, name="flaky", status="failed", stage="test", allow_failure=True),
        ]
    )
    await anotify_watch_exhausted(session=session, pipeline=pipeline)

    # Reading the pipeline is this module's job — the allow_failure job is not a real failure — and
    # everything else (keys, channels, dedup, wording) belongs to notifications/. The pipeline id
    # travels too: it is what scopes the dedup key to one give-up rather than to the whole thread.
    assert calls == [
        {"session": session, "failing_jobs": ["tests"], "pipeline_url": "https://example.com/p/100", "pipeline_id": 100}
    ]


@pytest.mark.django_db(transaction=True)
async def test_a_failing_emitter_never_breaks_the_watch(monkeypatch, django_user_model, caplog):
    from sessions.pipeline_watch import anotify_watch_exhausted

    user = await django_user_model.objects.acreate(username="owner2", email="owner2@example.com")
    session = await Session.objects.acreate(
        thread_id="t2", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", user=user
    )

    def boom(**_kwargs):
        raise RuntimeError("channel down")

    monkeypatch.setattr("notifications.watch_notifiers.emit_watch_exhausted", boom)

    # The MR comment is the reliable channel, so a notification failure must not propagate into
    # the state machine that has already recorded the watch as exhausted.
    await anotify_watch_exhausted(session=session, pipeline=make_pipeline())

    assert "failed to notify" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_the_notification_lands_under_the_shared_session_key(django_user_model):
    from sessions.pipeline_watch import anotify_watch_exhausted

    user = await django_user_model.objects.acreate(username="owner3", email="owner3@example.com")
    session = await Session.objects.acreate(
        thread_id="t3",
        origin=SessionOrigin.PIPELINE_WEBHOOK,
        repo_id="group/repo",
        merge_request_iid=9,
        watch_attempts=2,
        user=user,
    )

    await anotify_watch_exhausted(session=session, pipeline=make_pipeline())

    notification = await Notification.objects.aget()
    assert notification.event_type == EventType.PIPELINE_WATCH_EXHAUSTED
    assert notification.source_type == SOURCE_SESSION
    assert notification.source_id == "t3:100"
    assert notification.context["pipeline_url"] == "https://example.com/p/100"
