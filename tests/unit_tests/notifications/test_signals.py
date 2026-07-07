import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from notifications.choices import ChannelType, NotifyOn
from notifications.models import Notification, UserChannelBinding

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


@pytest.mark.django_db
class TestUserBindingSeeder:
    def test_creates_email_binding_on_user_create(self):
        user = User.objects.create_user(
            username="new",
            email="new@test.com",
            password="x",  # noqa: S106
        )
        assert UserChannelBinding.objects.filter(user=user, channel_type=ChannelType.EMAIL).count() == 1

        binding = UserChannelBinding.objects.get(user=user, channel_type=ChannelType.EMAIL)
        assert binding.address == "new@test.com"
        assert binding.is_verified is True
        assert binding.verified_at is not None

    def test_updates_binding_on_email_change(self):
        user = User.objects.create_user(
            username="u",
            email="a@test.com",
            password="x",  # noqa: S106
        )
        user.email = "b@test.com"
        user.save()

        binding = UserChannelBinding.objects.get(user=user, channel_type=ChannelType.EMAIL)
        assert binding.address == "b@test.com"

    def test_idempotent_on_repeated_save_same_email(self):
        user = User.objects.create_user(
            username="u",
            email="a@test.com",
            password="x",  # noqa: S106
        )
        user.name = "New Name"
        user.save()
        assert UserChannelBinding.objects.filter(user=user, channel_type=ChannelType.EMAIL).count() == 1

    def test_skips_user_without_email(self):
        user = User.objects.create_user(
            username="noemail",
            email="",
            password="x",  # noqa: S106
        )
        assert UserChannelBinding.objects.filter(user=user, channel_type=ChannelType.EMAIL).count() == 0


# Run-based notification tests (run_finished signal → on_run_finished receiver)
# ---------------------------------------------------------------------------


@pytest.fixture
def run_schedule(member_user, email_binding):
    return ScheduledJob.objects.create(
        user=member_user,
        name="run-schedule",
        prompt="p",
        repos=[{"repo_id": "x/y", "ref": ""}],
        frequency=Frequency.DAILY,
        time="12:00",
        notify_on=NotifyOn.ALWAYS,
    )


def _make_run_with_session(
    user=None,
    trigger_type="api_job",
    status="SUCCESSFUL",
    repo_id="x/y",
    scheduled_job=None,
    notify_on=None,
    batch_id=None,
    thread_id=None,
    input_tokens=None,
    output_tokens=None,
    total_tokens=None,
    cost_usd=None,
):
    from sessions.models import Run, Session, SessionOrigin

    origin = trigger_type if trigger_type != SessionOrigin.CHAT else SessionOrigin.CHAT
    session = Session.objects.create(
        thread_id=thread_id or str(uuid.uuid4()), origin=origin, repo_id=repo_id, user=user, scheduled_job=scheduled_job
    )
    run = Run.objects.create(
        session=session,
        trigger_type=trigger_type,
        repo_id=repo_id,
        status=status,
        user=user,
        notify_on=notify_on,
        batch_id=batch_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )
    return run


@pytest.mark.django_db
class TestOnRunFinished:
    def test_notifications_skip_chat_runs(self, member_user):
        """Chat-triggered runs must never produce notifications."""
        from sessions.models import RunStatus, SessionOrigin
        from sessions.signals import emit_run_finished_if_terminal

        run = _make_run_with_session(user=member_user, trigger_type=SessionOrigin.CHAT, status=RunStatus.SUCCESSFUL)
        with patch("notifications.signals.notify") as mock_notify:
            emit_run_finished_if_terminal(run, previous_status=RunStatus.RUNNING)

        mock_notify.assert_not_called()
        assert Notification.objects.filter(recipient=member_user).count() == 0

    def test_notifications_skip_webhook_runs(self, member_user):
        from sessions.models import RunStatus, SessionOrigin
        from sessions.signals import emit_run_finished_if_terminal

        for trigger in (SessionOrigin.ISSUE_WEBHOOK, SessionOrigin.MR_WEBHOOK):
            run = _make_run_with_session(user=member_user, trigger_type=trigger, status=RunStatus.SUCCESSFUL)
            emit_run_finished_if_terminal(run, previous_status=RunStatus.RUNNING)

        assert Notification.objects.count() == 0

    def test_api_job_run_successful_creates_bell(self, member_user):
        from sessions.models import RunStatus, SessionOrigin
        from sessions.signals import emit_run_finished_if_terminal

        run = _make_run_with_session(user=member_user, trigger_type=SessionOrigin.API_JOB, status=RunStatus.SUCCESSFUL)
        emit_run_finished_if_terminal(run, previous_status=RunStatus.RUNNING)

        assert Notification.objects.filter(recipient=member_user, event_type="job.finished").count() == 1

    def test_schedule_run_notifies_owner(self, member_user, run_schedule):
        from sessions.models import RunStatus, SessionOrigin
        from sessions.signals import emit_run_finished_if_terminal

        run = _make_run_with_session(
            user=member_user,
            trigger_type=SessionOrigin.SCHEDULE,
            status=RunStatus.SUCCESSFUL,
            scheduled_job=run_schedule,
        )
        emit_run_finished_if_terminal(run, previous_status=RunStatus.RUNNING)

        assert Notification.objects.filter(recipient=member_user, event_type="schedule.finished").count() == 1

    def test_run_context_carries_metadata(self, member_user, run_schedule):
        from sessions.models import RunStatus, SessionOrigin
        from sessions.signals import emit_run_finished_if_terminal

        run = _make_run_with_session(
            user=member_user,
            trigger_type=SessionOrigin.SCHEDULE,
            status=RunStatus.SUCCESSFUL,
            scheduled_job=run_schedule,
            repo_id="acme/app",
            input_tokens=100,
            output_tokens=200,
            total_tokens=300,
            cost_usd=Decimal("0.05"),
        )
        emit_run_finished_if_terminal(run, previous_status=RunStatus.RUNNING)

        n = Notification.objects.get(recipient=member_user, event_type="schedule.finished")
        assert n.context["repo_id"] == "acme/app"
        assert n.context["status"] == RunStatus.SUCCESSFUL
        assert n.context["input_tokens"] == 100
