from __future__ import annotations

import pytest
from notifications.channels.registry import enabled_channel_types
from notifications.choices import EventType
from notifications.models import Notification
from notifications.policy import SOURCE_SESSION
from notifications.watch_notifiers import emit_watch_exhausted
from sessions.models import Session, SessionOrigin

pytestmark = pytest.mark.django_db


@pytest.fixture
def watch_session(member_user):
    return Session.objects.create(
        thread_id="repo-mr-7",
        origin=SessionOrigin.PIPELINE_WEBHOOK,
        repo_id="group/repo",
        merge_request_iid=7,
        watch_attempts=3,
        user=member_user,
    )


def _emit(session, jobs=("tests", "lint"), pipeline_id=100):
    emit_watch_exhausted(
        session=session, failing_jobs=list(jobs), pipeline_url="https://ci.example.com/p/100", pipeline_id=pipeline_id
    )


class TestSourceKey:
    def test_it_keys_on_the_shared_session_source_key(self, watch_session):
        # The key must come from policy.py, not a bare "session" literal spelled at the call site,
        # so the emit and any re-drive resolve the same row.
        _emit(watch_session)

        notification = Notification.objects.get()
        assert notification.source_type == SOURCE_SESSION
        assert notification.source_id == f"{watch_session.thread_id}:100"
        assert notification.event_type == EventType.PIPELINE_WATCH_EXHAUSTED

    def test_the_session_source_key_is_namespaced_like_its_siblings(self):
        assert SOURCE_SESSION == "sessions.Session"


class TestChannels:
    def test_it_delivers_on_every_enabled_channel(self, watch_session, email_binding):
        _emit(watch_session)

        notification = Notification.objects.get()
        assert sorted(notification.deliveries.values_list("channel_type", flat=True)) == sorted(enabled_channel_types())


class TestDedup:
    def test_a_raced_double_emit_for_one_pipeline_delivers_once(self, watch_session):
        # A raced pipeline event and a reconciler sweep can both reach the exhaustion branch, which
        # does not compare-and-swap. The partial unique constraint is the only thing that stops two.
        _emit(watch_session)
        _emit(watch_session)

        assert Notification.objects.count() == 1

    def test_a_later_watch_cycle_on_the_same_merge_request_delivers_again(self, watch_session):
        # aarm_watch re-opens an exhausted watch and resets the budget, so one merge request can
        # legitimately give up more than once. Keying on thread_id alone muted every later one and
        # logged it as a benign race, while the MR comment still posted — the two channels
        # disagreeing is the tell.
        _emit(watch_session, pipeline_id=100)
        _emit(watch_session, pipeline_id=205)

        assert Notification.objects.count() == 2

    def test_the_constraint_covers_this_event(self):
        from django.contrib.auth import get_user_model
        from django.db import IntegrityError

        user = get_user_model().objects.create(username="dup", email="dup@example.com")
        kwargs = {
            "recipient": user,
            "event_type": EventType.PIPELINE_WATCH_EXHAUSTED,
            "source_type": SOURCE_SESSION,
            "source_id": "t",
            "subject": "s",
            "body": "b",
        }
        Notification.objects.create(**kwargs)

        with pytest.raises(IntegrityError):
            Notification.objects.create(**kwargs)


class TestPayload:
    def test_it_links_to_the_session_page_not_the_external_pipeline_url(self, watch_session):
        # link_url is run through build_absolute_url by both the email channel and the Rocket Chat
        # renderer, which prefix the site domain. An absolute CI URL there renders as
        # "https://<daiv>https://<ci>/..." — so the pipeline URL travels in the context instead.
        _emit(watch_session)

        notification = Notification.objects.get()
        assert notification.link_url.startswith("/")
        assert "ci.example.com" not in notification.link_url
        assert notification.context["pipeline_url"] == "https://ci.example.com/p/100"

    def test_it_carries_the_context_the_renderer_and_email_read(self, watch_session):
        _emit(watch_session)

        ctx = Notification.objects.get().context
        assert ctx["status_tone"] == "failure"
        assert ctx["status_label"]
        assert ctx["repo_id"] == "group/repo"
        assert ctx["merge_request_iid"] == 7
        assert ctx["attempts"] == 3
        assert ctx["failing_jobs"] == ["tests", "lint"]

    def test_the_failing_jobs_reach_the_body(self, watch_session):
        _emit(watch_session)

        body = Notification.objects.get().body
        assert "tests, lint" in body
        assert "3" in body

    def test_an_empty_job_list_still_names_something(self, watch_session):
        _emit(watch_session, jobs=())

        assert "the pipeline" in Notification.objects.get().body


class TestNoRecipient:
    def test_a_session_with_no_owner_notifies_nobody(self, caplog):
        session = Session.objects.create(
            thread_id="ownerless", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", user=None
        )

        _emit(session)

        # Webhook-origin sessions frequently have no DAIV user. The MR comment is the reliable
        # channel, so this degrades quietly rather than raising.
        assert Notification.objects.count() == 0
        assert "no recipient" in caplog.text.lower()
