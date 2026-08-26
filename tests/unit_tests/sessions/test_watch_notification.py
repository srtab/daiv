import pytest
from notifications.choices import EventType
from sessions.models import Session, SessionOrigin

from .conftest import make_pipeline


@pytest.mark.django_db(transaction=True)
async def test_it_notifies_the_session_owner(monkeypatch, django_user_model):
    from sessions.pipeline_watch import anotify_watch_exhausted

    user = await django_user_model.objects.acreate(username="owner", email="owner@example.com")
    session = await Session.objects.acreate(
        thread_id="t", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", user=user
    )

    sent = []
    monkeypatch.setattr("sessions.pipeline_watch.notify", lambda **kwargs: sent.append(kwargs))

    await anotify_watch_exhausted(session=session, pipeline=make_pipeline())

    assert sent[0]["recipient"] == user
    assert sent[0]["event_type"] == EventType.PIPELINE_WATCH_EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_a_session_with_no_owner_notifies_nobody(monkeypatch, caplog):
    from sessions.pipeline_watch import anotify_watch_exhausted

    session = await Session.objects.acreate(
        thread_id="t2", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", user=None
    )

    sent = []
    monkeypatch.setattr("sessions.pipeline_watch.notify", lambda **kwargs: sent.append(kwargs))

    await anotify_watch_exhausted(session=session, pipeline=make_pipeline())

    # Webhook-origin sessions frequently have no DAIV user. The MR comment is the reliable
    # channel; this must degrade quietly rather than raise.
    assert sent == []
    assert "no recipient" in caplog.text.lower()
