import uuid
from datetime import time
from unittest import mock

from django.test import Client
from django.urls import reverse

import pytest
from activity.models import Activity, TriggerType
from django_tasks_db.models import DBTaskResult, get_date_max

from accounts.models import User
from schedules.models import ScheduledJob


@pytest.fixture
def schedule(member_user):
    job = ScheduledJob(
        user=member_user,
        name="Daily review",
        prompt="Review open merge requests.",
        repo_id="owner/repo",
        ref="",
        repos=[{"repo_id": "owner/repo", "ref": ""}],
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


@pytest.mark.django_db(transaction=True)
class TestScheduleRunNowView:
    @staticmethod
    def _make_task_row():
        tid = uuid.uuid4()
        DBTaskResult.objects.create(
            id=tid,
            status="READY",
            task_path="jobs.tasks.run_job_task",
            args_kwargs={"args": [], "kwargs": {}},
            queue_name="default",
            backend_name="default",
            run_after=get_date_max(),
            return_value={},
        )
        m = mock.MagicMock()
        m.id = tid
        return m

    @staticmethod
    async def _amake_task_row():
        tid = uuid.uuid4()
        await DBTaskResult.objects.acreate(
            id=tid,
            status="READY",
            task_path="jobs.tasks.run_job_task",
            args_kwargs={"args": [], "kwargs": {}},
            queue_name="default",
            backend_name="default",
            run_after=get_date_max(),
            return_value={},
        )
        m = mock.MagicMock()
        m.id = tid
        return m

    def test_enqueues_single_repo_and_redirects_to_activity_detail(self, member_client, member_user, schedule):
        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=self._make_task_row())
            response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]))

        assert response.status_code == 302
        activity = Activity.objects.get(scheduled_job=schedule)
        assert activity.trigger_type == TriggerType.SCHEDULE
        assert activity.batch_id is not None
        assert response.url == reverse("activity_detail", args=[activity.pk])

    def test_multi_repo_redirects_to_batch_filtered_activity_list(self, member_client, member_user, schedule):
        schedule.repos = [{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": ""}]
        schedule.save(update_fields=["repos"])

        async def _aenq(**kwargs):
            return await self._amake_task_row()

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue.side_effect = _aenq
            response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]))

        assert response.status_code == 302
        assert "batch=" in response.url
        activities = list(Activity.objects.filter(scheduled_job=schedule))
        assert len(activities) == 2
        assert len({a.batch_id for a in activities}) == 1

    def test_works_on_disabled_schedule(self, member_client, schedule):
        schedule.is_enabled = False
        schedule.next_run_at = None
        schedule.save(update_fields=["is_enabled", "next_run_at"])

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=self._make_task_row())
            response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]))

        assert response.status_code == 302
        activity = Activity.objects.get(scheduled_job=schedule)
        assert response.url == reverse("activity_detail", args=[activity.pk])

    def test_enqueue_failure_returns_error_message(self, member_client, schedule):
        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(side_effect=RuntimeError("backend down"))
            response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]), follow=True)

        # All repos failed → no activities, redirect to schedule_list with a warning message.
        assert response.status_code == 200
        assert not Activity.objects.filter(scheduled_job=schedule).exists()
        content = response.content.decode()
        assert "triggered with failures" in content or "Failed to trigger" in content


@pytest.mark.django_db
class TestScheduleCreateViewSubscribers:
    def test_owner_passed_to_form_on_create(self, member_client, member_user):
        import json as _json

        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        payload = {
            "name": "Daily",
            "prompt": "p",
            "repos_json": _json.dumps([{"repo_id": "x/y", "ref": ""}]),
            "frequency": "daily",
            "cron_expression": "",
            "time": "09:00",
            "use_max": "false",
            "notify_on": "never",
            "subscribers": [str(alice.pk)],
        }
        response = member_client.post(reverse("schedule_create"), data=payload)
        assert response.status_code in (302, 200), response.content.decode()[:400]
        schedule = ScheduledJob.objects.get(name="Daily")
        assert list(schedule.subscribers.all()) == [alice]
        assert schedule.user == member_user

    def test_owner_rejected_as_own_subscriber_on_create(self, member_client, member_user):
        import json as _json

        payload = {
            "name": "Daily",
            "prompt": "p",
            "repos_json": _json.dumps([{"repo_id": "x/y", "ref": ""}]),
            "frequency": "daily",
            "cron_expression": "",
            "time": "09:00",
            "use_max": "false",
            "notify_on": "never",
            "subscribers": [str(member_user.pk)],
        }
        response = member_client.post(reverse("schedule_create"), data=payload)
        assert response.status_code == 200
        assert not ScheduledJob.objects.filter(name="Daily").exists()


@pytest.mark.django_db
class TestScheduleUpdateViewSubscribers:
    def test_update_form_prefills_selected_subscribers(self, member_client, schedule):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        schedule.subscribers.add(alice)
        response = member_client.get(reverse("schedule_update", args=[schedule.pk]))
        html = response.content.decode()
        assert "alice" in html

    def test_create_form_renders_picker_markers(self, member_client):
        response = member_client.get(reverse("schedule_create"))
        html = response.content.decode()
        assert 'id="id_subscribers"' in html
        assert "subscriberPicker" in html
        assert "Subscribers" in html

    def test_owner_passed_to_form_on_update(self, member_client, schedule):
        import json as _json

        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        payload = {
            "name": schedule.name,
            "prompt": schedule.prompt,
            "repos_json": _json.dumps([{"repo_id": schedule.repo_id, "ref": schedule.ref}]),
            "frequency": schedule.frequency,
            "cron_expression": "",
            "time": "09:00",
            "use_max": "false",
            "notify_on": "never",
            "is_enabled": "true",
            "subscribers": [str(alice.pk)],
        }
        response = member_client.post(reverse("schedule_update", args=[schedule.pk]), data=payload)
        assert response.status_code in (302, 200), response.content.decode()[:400]
        schedule.refresh_from_db()
        assert list(schedule.subscribers.all()) == [alice]


@pytest.mark.django_db
class TestScheduleUnsubscribeView:
    def _subscriber(self, username="sub1"):
        return User.objects.create_user(username=username, email=f"{username}@t.com", password="x")  # noqa: S106

    def test_subscriber_can_unsubscribe(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)

        client = Client()
        client.force_login(sub)
        response = client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 302
        schedule.refresh_from_db()
        assert sub not in schedule.subscribers.all()

    def test_non_subscriber_gets_404(self, schedule):
        other = self._subscriber("other")
        client = Client()
        client.force_login(other)
        response = client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 404

    def test_owner_gets_404(self, member_client, schedule):
        response = member_client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 404

    def test_rejects_get(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 405

    def test_next_redirect_honored_when_safe(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)
        client = Client()
        client.force_login(sub)
        response = client.post(
            reverse("schedule_unsubscribe", args=[schedule.pk]), data={"next": "/dashboard/activity/"}
        )
        assert response.status_code == 302
        assert response.url == "/dashboard/activity/"

    def test_unsafe_next_falls_back_to_activity_list(self, schedule):
        sub = self._subscriber()
        schedule.subscribers.add(sub)
        client = Client()
        client.force_login(sub)
        response = client.post(
            reverse("schedule_unsubscribe", args=[schedule.pk]), data={"next": "https://evil.example.com/phish"}
        )
        assert response.status_code == 302
        assert response.url == reverse("activity_list")

    def test_unauthenticated_redirects_to_login(self, schedule):
        client = Client()
        response = client.post(reverse("schedule_unsubscribe", args=[schedule.pk]))
        assert response.status_code == 302
        assert "/login" in response.url or "/accounts/" in response.url

    def test_nonexistent_schedule_returns_404(self, member_client):
        response = member_client.post(reverse("schedule_unsubscribe", args=[99999]))
        assert response.status_code == 404
