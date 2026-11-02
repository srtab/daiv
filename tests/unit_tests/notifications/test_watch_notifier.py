from __future__ import annotations

from unittest.mock import AsyncMock, patch

from django.urls import reverse

import pytest
from notifications.channels.registry import enabled_channel_types
from notifications.choices import EventType
from notifications.models import Notification
from notifications.policy import SOURCE_SESSION
from notifications.watch_notifiers import emit_watch_exhausted
from sessions.models import EventKind, EventState, InboxEvent, MergeRequestOwnership, Run, RunStatus, SessionOrigin

from tests.unit_tests.sessions.conftest import make_task_result

pytestmark = pytest.mark.django_db


def _emit(ownership, jobs=("tests", "lint"), sha="abc123"):
    emit_watch_exhausted(
        ownership=ownership,
        session=ownership.session,
        failing_jobs=list(jobs),
        pipeline_url="https://ci.example.com/p/100",
        sha=sha,
    )


class TestSourceKey:
    def test_it_keys_on_the_shared_session_source_key(self, watch_ownership):
        # The key must come from policy.py, not a bare "session" literal spelled at the call site,
        # so the emit and any re-drive resolve the same row.
        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert notification.source_type == SOURCE_SESSION
        assert notification.source_id == f"{watch_ownership.session_id}:abc123"
        assert notification.event_type == EventType.PIPELINE_WATCH_EXHAUSTED

    def test_the_session_source_key_is_namespaced_like_its_siblings(self):
        assert SOURCE_SESSION == "sessions.Session"


class TestChannels:
    def test_it_delivers_on_every_enabled_channel(self, watch_ownership, email_binding):
        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert sorted(notification.deliveries.values_list("channel_type", flat=True)) == sorted(enabled_channel_types())


class TestDedup:
    def test_a_retried_pipeline_on_the_same_commit_delivers_once(self, watch_ownership):
        _emit(watch_ownership)
        _emit(watch_ownership)

        assert Notification.objects.count() == 1

    def test_a_later_daiv_commit_that_also_runs_out_delivers_again(self, watch_ownership):
        _emit(watch_ownership, sha="abc123")
        _emit(watch_ownership, sha="def456")

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
    def test_it_links_to_the_session_page_not_the_external_pipeline_url(self, watch_ownership):
        # link_url is run through build_absolute_url by both the email channel and the Rocket Chat
        # renderer, which prefix the site domain. An absolute CI URL there renders as
        # "https://<daiv>https://<ci>/..." — so the pipeline URL travels in the context instead.
        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert notification.link_url.startswith("/")
        assert "ci.example.com" not in notification.link_url
        assert notification.context["pipeline_url"] == "https://ci.example.com/p/100"

    def test_it_carries_the_context_the_renderer_and_email_read(self, watch_ownership):
        _emit(watch_ownership)

        ctx = Notification.objects.get().context
        assert ctx["status_tone"] == "failure"
        assert ctx["status_label"]
        assert ctx["repo_id"] == "group/repo"
        assert ctx["merge_request_iid"] == 7
        assert ctx["attempts"] == 3
        assert ctx["failing_jobs"] == ["tests", "lint"]

    def test_the_failing_jobs_reach_the_body(self, watch_ownership):
        _emit(watch_ownership)

        body = Notification.objects.get().body
        assert "tests, lint" in body
        assert "3" in body

    def test_an_empty_job_list_still_names_something(self, watch_ownership):
        _emit(watch_ownership, jobs=())

        assert "the pipeline" in Notification.objects.get().body


def _ci_run(ownership, summary="", *, iid=7, status=RunStatus.SUCCESSFUL, **fields) -> Run:
    """A run that took a waking CI failure on merge request ``iid``."""
    run = Run.objects.create(
        session=ownership.session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=ownership.repo_id,
        status=status, result_summary=summary, **fields,
    )  # fmt: skip
    InboxEvent.objects.create(
        session=ownership.session, kind=EventKind.CI_FAILED, source_key=f"pipeline:{run.pk}", wake=True,
        merge_request_iid=iid, run=run, state=EventState.HANDLED,
        payload={"pipeline_id": 1, "sha": "abc123", "status": "failed", "web_url": "https://ci/1", "failed_jobs": []},
    )  # fmt: skip
    return run


class TestExplanation:
    """The owner hears why the watch stopped from the last run CI woke on the merge request (SC-007)."""

    def test_the_last_ci_woken_runs_summary_reaches_the_body_and_the_summary(self, watch_ownership):
        _ci_run(watch_ownership, "An older look.")
        _ci_run(watch_ownership, "The registry timed out; nothing in the code to fix.")

        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert "The registry timed out; nothing in the code to fix." in notification.body
        assert "An older look." not in notification.body
        assert notification.context["summary"] == "The registry timed out; nothing in the code to fix."

    def test_a_run_with_no_summary_explains_with_its_response(self, watch_ownership):
        _ci_run(watch_ownership, task_result=make_task_result(return_value={"response": "Flaky runner."}))

        _emit(watch_ownership)

        assert Notification.objects.get().context["summary"] == "Flaky runner."

    @pytest.mark.parametrize(
        "fields", [{"iid": 8}, {"status": RunStatus.RUNNING}], ids=["another-merge-request", "still-running"]
    )
    def test_only_a_finished_run_woken_by_this_merge_requests_ci_explains_it(self, watch_ownership, fields):
        _ci_run(watch_ownership, "Not this one.", **fields)

        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert "Not this one." not in notification.body
        assert "summary" not in notification.context

    def test_with_no_ci_woken_run_the_notification_still_goes_out(self, watch_ownership):
        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert "tests, lint" in notification.body
        assert "summary" not in notification.context

    def test_repository_paths_in_the_explanation_are_relative_to_the_repository(self, watch_ownership):
        _ci_run(watch_ownership, "The lockfile /workspace/repo/uv.lock pins an old index.")

        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert "The lockfile uv.lock pins an old index." in notification.body
        assert notification.context["summary"] == "The lockfile uv.lock pins an old index."

    def test_an_ownerless_merge_requests_comment_leaves_the_explanation_to_the_runs_own_comment(self, watch_ownership):
        """The run's final message is already on the merge request, posted when the run ended."""
        _ci_run(watch_ownership, "Flaky runner.")
        MergeRequestOwnership.objects.filter(pk=watch_ownership.pk).update(owner=None)
        watch_ownership.refresh_from_db()

        with patch("sessions.pipeline_watch.platform.WatchPlatform.apost_note", new=AsyncMock()) as note:
            _emit(watch_ownership)

        assert "Flaky runner." not in note.await_args.kwargs["body"]


class TestRecipient:
    def test_it_is_the_owner_of_the_merge_request_and_links_the_owning_session(self, watch_ownership, member_user):
        _emit(watch_ownership)

        notification = Notification.objects.get()
        assert notification.recipient == member_user
        assert notification.link_url == reverse("session_detail", kwargs={"thread_id": watch_ownership.session_id})

    def test_an_ownerless_merge_request_gets_the_outcome_as_a_comment(self, watch_ownership):
        MergeRequestOwnership.objects.filter(pk=watch_ownership.pk).update(owner=None)
        watch_ownership.refresh_from_db()

        with patch("sessions.pipeline_watch.platform.WatchPlatform.apost_note", new=AsyncMock()) as note:
            _emit(watch_ownership)

        assert Notification.objects.count() == 0
        note.assert_awaited_once()
        assert note.await_args.kwargs["merge_request_iid"] == 7
        assert "tests, lint" in note.await_args.kwargs["body"]
        assert "https://ci.example.com/p/100" in note.await_args.kwargs["body"]
