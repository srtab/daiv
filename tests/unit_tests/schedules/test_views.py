import uuid
from datetime import time
from unittest import mock

from django.test import Client
from django.urls import reverse

import pytest
from activity.models import Activity, TriggerType
from django_tasks_db.models import DBTaskResult, get_date_max
from notifications.choices import NotifyOn

from accounts.models import User
from schedules.models import Frequency, ScheduledJob, ScheduleTemplate


@pytest.fixture
def schedule(member_user):
    job = ScheduledJob(
        user=member_user,
        name="Daily review",
        prompt="Review open merge requests.",
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

    def test_resume_fired_one_off_is_rejected(self, member_client, member_user):
        from datetime import timedelta

        from django.utils import timezone

        past = timezone.now() - timedelta(hours=1)
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="fired one-off",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=past,
            is_enabled=False,
            next_run_at=None,
            run_count=1,
            last_run_at=past,
        )
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "schedule-toggle-error"
        schedule.refresh_from_db()
        assert schedule.is_enabled is False

    def test_resume_stale_one_off_is_rejected(self, member_client, member_user):
        """A paused-then-stale ONCE (run_count=0, run_at in the past) cannot be re-armed.

        Without this guard the toggle's ``compute_next_run`` would set ``next_run_at = run_at``
        in the past, causing the dispatcher to fire immediately on the next minute tick.
        """
        from datetime import timedelta

        from django.utils import timezone

        past = timezone.now() - timedelta(hours=1)
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="stale one-off",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=past,
            is_enabled=False,
            next_run_at=None,
            run_count=0,
        )
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "schedule-toggle-error"
        schedule.refresh_from_db()
        assert schedule.is_enabled is False
        assert schedule.next_run_at is None

    def test_resume_paused_future_one_off_succeeds(self, member_client, member_user):
        """A paused ONCE with run_at still in the future is a legitimate resume."""
        from datetime import timedelta

        from django.utils import timezone

        future = timezone.now() + timedelta(hours=2)
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="paused future one-off",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=future,
            is_enabled=False,
            next_run_at=None,
            run_count=0,
        )
        response = member_client.post(reverse("schedule_toggle", args=[schedule.pk]))
        assert response.status_code == 200
        assert "HX-Trigger" not in response.headers
        schedule.refresh_from_db()
        assert schedule.is_enabled is True
        assert schedule.next_run_at == future


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

    def test_run_now_persists_explicit_env_on_activity(self, member_client, member_user, schedule):
        """Schedule with an explicit env → run-now stamps that env on the generated Activity."""
        from sandbox_envs.models import SandboxEnvironment, Scope

        env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="prod", base_image="x")
        schedule.sandbox_environment = env
        schedule.save(update_fields=["sandbox_environment"])

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=self._make_task_row())
            response = member_client.post(reverse("schedule_run_now", args=[schedule.pk]))

        assert response.status_code == 302
        activity = Activity.objects.get(scheduled_job=schedule)
        assert activity.sandbox_environment_id == env.id

    def test_run_now_auto_resolves_against_schedule_owner_not_request_user(
        self, member_client, member_user, schedule, admin_user
    ):
        """ScheduleRunNowView.post intentionally resolves Auto against ``schedule.user``,
        not ``request.user`` — locks parity with the cron dispatcher. If a future change
        switches to request.user, this test fails."""
        from sandbox_envs.models import SandboxEnvironment, Scope

        SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True)
        # Owner's USER env claiming the schedule's repo; admin (request.user) has none.
        owner_env = SandboxEnvironment.objects.create(
            scope=Scope.USER, user=member_user, name="owner-env", base_image="x", repo_ids=["owner/repo"]
        )

        from django.test import Client

        admin_client = Client()
        admin_client.force_login(admin_user)
        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=self._make_task_row())
            response = admin_client.post(reverse("schedule_run_now", args=[schedule.pk]))

        assert response.status_code == 302
        activity = Activity.objects.get(scheduled_job=schedule)
        assert activity.sandbox_environment_id == owner_env.id


@pytest.mark.django_db
class TestScheduleCreateViewSubscribers:
    def test_owner_passed_to_form_on_create(self, member_client, member_user):
        import json as _json

        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        payload = {
            "name": "Daily",
            "prompt": "p",
            "repos": _json.dumps([{"repo_id": "x/y", "ref": ""}]),
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
            "repos": _json.dumps([{"repo_id": "x/y", "ref": ""}]),
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

    def test_subscriber_initial_json_carries_avatar_fields(self, member_client, schedule):
        # The Alpine chip in ``_subscriber_picker.html`` reads ``u.initials`` and
        # ``u.color_index`` — losing them from the payload would silently break
        # the rendered avatar without failing any other test.
        import html as _html
        import json as _json
        import re

        alice = User.objects.create_user(username="alice", email="a@t.com", password="x", name="Alice Doe")  # noqa: S106
        schedule.subscribers.add(alice)
        page = member_client.get(reverse("schedule_update", args=[schedule.pk])).content.decode()
        # `x-data="subscriberPicker({ initial: [...] })"` — JSON's double quotes
        # get HTML-escaped to `&quot;` inside the attribute, so unescape first.
        match = re.search(r"subscriberPicker\(\{\s*initial:\s*(\[.*?\])\s*\}\)", _html.unescape(page))
        assert match, "subscriberPicker x-data init not found in rendered HTML"
        rows = _json.loads(match.group(1))
        assert len(rows) == 1
        assert rows[0]["initials"] == "AD"
        assert isinstance(rows[0]["color_index"], int)
        assert 0 <= rows[0]["color_index"] < 10

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
            "repos": _json.dumps(schedule.repos),
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


@pytest.mark.django_db
class TestScheduleListViewTemplateContext:
    """The schedule list view exposes the same template payload for the gallery."""

    def test_includes_templates_when_present(self, member_client, admin_user):
        ScheduleTemplate.objects.create(
            name="Nightly scan",
            description="Runs nightly.",
            prompt="Scan.",
            frequency=Frequency.HOURLY,
            notify_on=NotifyOn.NEVER,
            created_by=admin_user,
        )
        response = member_client.get(reverse("schedule_list"))
        assert response.status_code == 200
        [row] = response.context["schedule_templates"]
        assert row["name"] == "Nightly scan"
        assert row["frequency_summary"] == "Every hour"

    def test_empty_when_no_templates(self, member_client):
        response = member_client.get(reverse("schedule_list"))
        assert response.status_code == 200
        assert response.context["schedule_templates"] == []

    def test_orders_templates_by_usage_count_then_name(self, member_client, member_user, admin_user):
        popular = ScheduleTemplate.objects.create(
            name="Popular", prompt="p", frequency=Frequency.HOURLY, notify_on=NotifyOn.NEVER, created_by=admin_user
        )
        ScheduleTemplate.objects.create(
            name="Zebra", prompt="p", frequency=Frequency.HOURLY, notify_on=NotifyOn.NEVER, created_by=admin_user
        )
        ScheduleTemplate.objects.create(
            name="Alpha", prompt="p", frequency=Frequency.HOURLY, notify_on=NotifyOn.NEVER, created_by=admin_user
        )
        ScheduledJob.objects.create(
            user=member_user,
            name="From popular",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency="hourly",
            source_template=popular,
        )
        response = member_client.get(reverse("schedule_list"))
        names = [row["name"] for row in response.context["schedule_templates"]]
        assert names == ["Popular", "Alpha", "Zebra"]

    def test_tied_usage_counts_break_alphabetically(self, member_client, member_user, admin_user):
        # Locks the secondary ``name`` sort: a refactor to ``order_by("-usage_count")``
        # alone would silently regress without this assertion.
        beta = ScheduleTemplate.objects.create(
            name="Beta", prompt="p", frequency=Frequency.HOURLY, notify_on=NotifyOn.NEVER, created_by=admin_user
        )
        alpha = ScheduleTemplate.objects.create(
            name="Alpha", prompt="p", frequency=Frequency.HOURLY, notify_on=NotifyOn.NEVER, created_by=admin_user
        )
        for tpl in (alpha, beta):
            ScheduledJob.objects.create(
                user=member_user,
                name=f"From {tpl.name}",
                prompt="p",
                repos=[{"repo_id": "x/y", "ref": ""}],
                frequency="hourly",
                source_template=tpl,
            )
        response = member_client.get(reverse("schedule_list"))
        names = [row["name"] for row in response.context["schedule_templates"]]
        assert names == ["Alpha", "Beta"]

    def test_picker_payload_is_single_query(self, member_client, admin_user, django_assert_num_queries):
        # PICKER_FIELDS must cover every field ``to_picker_dict()`` reads, or ``.only(...)``
        # triggers a deferred-field SELECT per row. Pins the contract called out at
        # ``schedules/models.py`` PICKER_FIELDS to catch silent regressions.
        for name in ("A", "B", "C"):
            ScheduleTemplate.objects.create(
                name=name, prompt="p", frequency=Frequency.HOURLY, notify_on=NotifyOn.NEVER, created_by=admin_user
            )
        from schedules.views import _template_picker_payload

        with django_assert_num_queries(1):
            payload = _template_picker_payload()
        assert len(payload) == 3


@pytest.mark.django_db
class TestScheduleCreateViewSourceTemplate:
    def _payload(self, **overrides):
        import json as _json

        base = {
            "name": "From tpl",
            "prompt": "p",
            "repos": _json.dumps([{"repo_id": "x/y", "ref": ""}]),
            "frequency": "daily",
            "cron_expression": "",
            "time": "09:00",
            "use_max": "false",
            "notify_on": "never",
        }
        base.update(overrides)
        return base

    def test_source_template_set_when_query_param_present(self, member_client, admin_user):
        tpl = ScheduleTemplate.objects.create(
            name="T", prompt="p", frequency=Frequency.DAILY, time=time(9, 0), created_by=admin_user
        )
        url = f"{reverse('schedule_create')}?template={tpl.pk}"
        response = member_client.post(url, data=self._payload())
        assert response.status_code == 302, response.content.decode()[:400]
        schedule = ScheduledJob.objects.get(name="From tpl")
        assert schedule.source_template_id == tpl.pk

    def test_source_template_null_when_no_query_param(self, member_client):
        response = member_client.post(reverse("schedule_create"), data=self._payload())
        assert response.status_code == 302, response.content.decode()[:400]
        schedule = ScheduledJob.objects.get(name="From tpl")
        assert schedule.source_template_id is None

    def test_source_template_null_when_query_param_invalid(self, member_client):
        url = f"{reverse('schedule_create')}?template=99999"
        response = member_client.post(url, data=self._payload())
        assert response.status_code == 302, response.content.decode()[:400]
        schedule = ScheduledJob.objects.get(name="From tpl")
        assert schedule.source_template_id is None


@pytest.mark.django_db
class TestScheduleListViewGalleryWiring:
    """The schedule list page renders the gallery trigger and empty-state CTA."""

    @pytest.fixture
    def tpl(self, admin_user):
        return ScheduleTemplate.objects.create(
            name="Nightly scan",
            description="Runs nightly.",
            prompt="Scan.",
            frequency=Frequency.DAILY,
            time=time(2, 0),
            notify_on=NotifyOn.NEVER,
            created_by=admin_user,
        )

    def test_header_button_and_gallery_present_when_templates_exist(self, member_client, tpl):
        response = member_client.get(reverse("schedule_list"))
        body = response.content.decode()
        assert "From template" in body
        assert "schedule-templates-data" in body

    def test_header_button_absent_when_no_templates(self, member_client):
        response = member_client.get(reverse("schedule_list"))
        body = response.content.decode()
        assert "From template" not in body
        assert "schedule-templates-data" not in body

    def test_empty_state_shows_template_cta_when_templates_exist(self, member_client, tpl):
        response = member_client.get(reverse("schedule_list"))
        body = response.content.decode()
        assert "No scheduled jobs yet" in body
        assert "Start from template" in body

    def test_empty_state_without_templates_keeps_only_create_button(self, member_client):
        response = member_client.get(reverse("schedule_list"))
        body = response.content.decode()
        assert "No scheduled jobs yet" in body
        assert "Start from template" not in body

    def test_gallery_apply_url_matches_prefill_contract(self, member_client, tpl):
        # Pins the gallery <-> prefill contract: the drawer's "Use this template"
        # anchor must navigate to the URL that ScheduleCreateView's ?template=<id>
        # prefill path accepts. Renamed params or routes would break both sides
        # silently without this assertion.
        response = member_client.get(reverse("schedule_list"))
        body = response.content.decode()
        assert f"{reverse('schedule_create')}?template=${{tpl.id}}" in body


@pytest.mark.django_db
class TestScheduleDuplicateFlow:
    def test_create_view_with_from_param_prefills(self, member_client, member_user):
        from datetime import timedelta

        from django.utils import timezone

        future = timezone.now() + timedelta(hours=2)
        source = ScheduledJob.objects.create(
            user=member_user,
            name="source",
            prompt="hello",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=future,
            use_max=True,
        )
        response = member_client.get(reverse("schedule_create") + f"?from={source.pk}")
        assert response.status_code == 200
        content = response.content.decode()
        assert "source" in content
        assert "hello" in content

    def test_create_view_with_unknown_from_pk_falls_back_to_blank(self, member_client):
        response = member_client.get(reverse("schedule_create") + "?from=999999")
        assert response.status_code == 200

    def test_create_view_from_param_for_other_user_falls_back_to_blank_form(self, member_user):
        """``?from=<other_user_pk>`` on the create view does NOT 404; it silently degrades.

        The owner-scoped ``_get_source_schedule`` returns ``None`` so the form renders
        empty, preventing information leaks via the create form. The hard 404 boundary
        lives on ``ScheduleDuplicateView`` (see sibling test).
        """
        from django.test import Client

        other = User.objects.create_user(username="other", email="o@t.com", password="x")  # noqa: S106
        future_job = ScheduledJob.objects.create(
            user=other,
            name="theirs",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency="daily",
            time="09:00",
        )
        c = Client()
        c.force_login(User.objects.create_user(username="me", email="me@t.com", password="x"))  # noqa: S106
        response = c.get(reverse("schedule_create") + f"?from={future_job.pk}")
        assert response.status_code == 200
        assert "theirs" not in response.content.decode()

    def test_duplicate_view_redirects_to_create_with_from_param(self, member_client, member_user):
        source = ScheduledJob.objects.create(
            user=member_user,
            name="source",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency="daily",
            time="09:00",
        )
        response = member_client.post(reverse("schedule_duplicate", args=[source.pk]))
        assert response.status_code == 302
        assert response.url == reverse("schedule_create") + f"?from={source.pk}"

    def test_duplicate_view_for_other_user_returns_404(self, member_user):
        """The actual cross-user security boundary lives on the duplicate POST endpoint."""
        from django.test import Client

        other = User.objects.create_user(username="other2", email="o2@t.com", password="x")  # noqa: S106
        source = ScheduledJob.objects.create(
            user=other,
            name="theirs",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency="daily",
            time="09:00",
        )
        c = Client()
        c.force_login(User.objects.create_user(username="me2", email="me2@t.com", password="x"))  # noqa: S106
        response = c.post(reverse("schedule_duplicate", args=[source.pk]))
        assert response.status_code == 404

    def test_create_view_from_fired_one_off_carries_stale_run_at_form_rejects(self, member_client, member_user):
        """Duplicating a fired one-off prefills the past run_at; submission without changing it must fail."""
        from datetime import timedelta

        from django.utils import timezone

        past = timezone.now() - timedelta(hours=1)
        fired = ScheduledJob.objects.create(
            user=member_user,
            name="fired-source",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=past,
            is_enabled=False,
            next_run_at=None,
            run_count=1,
            last_run_at=past,
        )

        # GET prefills the past run_at into the form initial.
        response = member_client.get(reverse("schedule_create") + f"?from={fired.pk}")
        assert response.status_code == 200
        assert response.context["form"].initial["run_at"] == past

        # POST without changing run_at must be rejected by the future-time validator.
        response = member_client.post(
            reverse("schedule_create"),
            data={
                "name": "duplicate",
                "prompt": "p",
                "repos": '[{"repo_id": "x/y", "ref": ""}]',
                "frequency": Frequency.ONCE,
                "cron_expression": "",
                "time": "",
                "run_at": past.strftime("%Y-%m-%dT%H:%M"),
                "use_max": False,
                "notify_on": NotifyOn.NEVER,
            },
        )
        assert response.status_code == 200
        assert "run_at" in response.context["form"].errors


@pytest.mark.django_db
class TestScheduleViewsEnvContext:
    def test_create_view_provides_sandbox_envs(self, member_client):
        response = member_client.get(reverse("schedule_create"))
        assert response.status_code == 200
        assert "sandbox_envs" in response.context
        envs = list(response.context["sandbox_envs"])
        assert any(e.scope == "global" and e.is_default for e in envs)
        assert response.context["selected_sandbox_env_id"] == ""

    def test_update_view_provides_sandbox_envs(self, member_client, member_user):
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="nightly",
            prompt="ping",
            repos=[{"repo_id": "r/x", "ref": "main"}],
            frequency=Frequency.DAILY,
            time="03:00:00",
            notify_on="never",
        )

        response = member_client.get(reverse("schedule_update", args=[schedule.pk]))
        assert response.status_code == 200
        assert "sandbox_envs" in response.context
        envs = list(response.context["sandbox_envs"])
        assert any(e.scope == "global" and e.is_default for e in envs)
        assert response.context["selected_sandbox_env_id"] == ""

    def test_update_view_preselects_saved_env(self, member_client, member_user):
        from sandbox_envs.models import SandboxEnvironment, Scope

        env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="dev", base_image="alpine")
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="nightly",
            prompt="ping",
            repos=[{"repo_id": "r/x", "ref": "main"}],
            frequency=Frequency.DAILY,
            time="03:00:00",
            notify_on="never",
            sandbox_environment=env,
        )

        response = member_client.get(reverse("schedule_update", args=[schedule.pk]))
        assert response.status_code == 200
        assert response.context["selected_sandbox_env_id"] == str(env.id)
