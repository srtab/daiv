import uuid
from datetime import time

from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import User
from schedules.models import ScheduledJob


@pytest.fixture
def schedule(member_user):
    job = ScheduledJob(
        user=member_user,
        name="Daily review",
        prompt="Review open merge requests.",
        repo_id="owner/repo",
        frequency="daily",
        time=time(9, 0),
        is_enabled=True,
    )
    job.compute_next_run()
    job.save()
    return job


@pytest.mark.django_db
class TestScheduleCreateView:
    def test_form_renders_notify_on_field(self, member_client):
        response = member_client.get(reverse("schedule_create"))
        assert response.status_code == 200
        assert "notify_on" in response.content.decode()


@pytest.mark.django_db
class TestScheduleToggleView:
    def test_pause_enabled_schedule(self, member_client, schedule):
        assert schedule.is_enabled is True
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 200
        schedule.refresh_from_db()
        assert schedule.is_enabled is False
        assert schedule.next_run_at is None

    def test_resume_disabled_schedule(self, member_client, schedule):
        schedule.is_enabled = False
        schedule.next_run_at = None
        schedule.save()
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 200
        schedule.refresh_from_db()
        assert schedule.is_enabled is True
        assert schedule.next_run_at is not None

    def test_returns_updated_row_html(self, member_client, schedule):
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        content = response.content.decode()
        assert "Paused" in content
        assert "Resume" in content

    def test_rejects_get(self, member_client, schedule):
        response = member_client.get(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 405

    def test_other_user_cannot_toggle(self, schedule):
        """A non-admin user other than the owner cannot toggle."""
        other = User.objects.create_user(username="other", email="other@test.com", password="testpass123")  # noqa: S106
        client = Client()
        client.force_login(other)
        response = client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 404

    def test_admin_can_toggle_any_schedule(self, admin_client, schedule):
        response = admin_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 200
        schedule.refresh_from_db()
        assert schedule.is_enabled is False

    def test_unauthenticated_redirects_to_login(self, schedule):
        client = Client()
        response = client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 302

    def test_nonexistent_schedule_returns_404(self, member_client):
        response = member_client.post(reverse("schedule_toggle", args=[99999]))
        assert response.status_code == 404

    def test_returns_active_html_after_resume(self, member_client, schedule):
        schedule.is_enabled = False
        schedule.next_run_at = None
        schedule.save()
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        content = response.content.decode()
        assert response["Content-Type"] == "text/html"
        assert f'id="schedule-{schedule.pk}"' in content
        assert "Active" in content
        assert "Pause" in content

    def test_resume_with_invalid_config_returns_unchanged_row(self, member_client, schedule):
        # Corrupt the schedule: daily frequency requires a time, but we null it out.
        # No DB constraint protects this, so the raw update succeeds.
        ScheduledJob.objects.filter(pk=schedule.pk).update(time=None, is_enabled=False, next_run_at=None)
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 200
        assert response["HX-Trigger"] == "schedule-toggle-error"
        schedule.refresh_from_db()
        assert schedule.is_enabled is False
        assert schedule.next_run_at is None


@pytest.mark.django_db
class TestScheduleRunNowView:
    def test_enqueues_job_and_creates_activity(self, member_client, member_user, schedule, mocker):
        mock_result = mocker.MagicMock()
        mock_result.id = uuid.uuid4()
        mock_task = mocker.patch("schedules.views.run_job_task")
        mock_task.enqueue = mocker.MagicMock(return_value=mock_result)
        mock_create = mocker.patch("schedules.views.create_activity")

        response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]))

        mock_task.enqueue.assert_called_once_with(
            repo_id=schedule.repo_id, prompt=schedule.prompt, ref=None, use_max=schedule.use_max
        )
        from activity.models import TriggerType

        mock_create.assert_called_once_with(
            trigger_type=TriggerType.SCHEDULE,
            task_result_id=mock_result.id,
            repo_id=schedule.repo_id,
            ref="",
            prompt=schedule.prompt,
            scheduled_job=schedule,
            user=member_user,
        )
        assert response.status_code == 302

        # Schedule tracking fields are unchanged
        schedule.refresh_from_db()
        assert schedule.run_count == 0

    def test_works_on_disabled_schedule(self, member_client, schedule, mocker):
        schedule.is_enabled = False
        schedule.next_run_at = None
        schedule.save(update_fields=["is_enabled", "next_run_at"])

        mock_result = mocker.MagicMock()
        mock_result.id = uuid.uuid4()
        mock_task = mocker.patch("schedules.views.run_job_task")
        mock_task.enqueue = mocker.MagicMock(return_value=mock_result)
        mock_create = mocker.patch("schedules.views.create_activity")

        response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]))
        assert response.status_code == 302
        mock_create.assert_called_once()

    def test_enqueue_failure_returns_error_message(self, member_client, schedule, mocker):
        mock_task = mocker.patch("schedules.views.run_job_task")
        mock_task.enqueue = mocker.MagicMock(side_effect=RuntimeError("backend down"))
        mock_create = mocker.patch("schedules.views.create_activity")

        response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]), follow=True)
        assert response.status_code == 200
        mock_create.assert_not_called()
        content = response.content.decode()
        assert "Failed to trigger" in content

    def test_activity_failure_still_succeeds(self, member_client, schedule, mocker):
        mock_result = mocker.MagicMock()
        mock_result.id = uuid.uuid4()
        mock_task = mocker.patch("schedules.views.run_job_task")
        mock_task.enqueue = mocker.MagicMock(return_value=mock_result)
        mocker.patch("schedules.views.create_activity", side_effect=RuntimeError("db error"))

        response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]), follow=True)
        assert response.status_code == 200
        content = response.content.decode()
        assert "triggered successfully" in content
