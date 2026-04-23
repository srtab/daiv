import uuid

from django.test import Client
from django.urls import reverse

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from django_tasks_db.models import DBTaskResult

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="testpass123")  # noqa: S106


@pytest.fixture
def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def _create_task_result(*, status="SUCCESSFUL", return_value=None):
    return DBTaskResult.objects.create(
        id=uuid.uuid4(),
        status=status,
        task_path="jobs.tasks.run_job_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after="9999-01-01T00:00:00Z",
        return_value=return_value or {},
        exception_class_path="",
        traceback="",
    )


def _create_activity(*, status=ActivityStatus.SUCCESSFUL, task_result=None, **kwargs):
    defaults = {
        "trigger_type": TriggerType.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
        "prompt": "Run a security audit",
        "status": status,
        "task_result": task_result,
    }
    defaults.update(kwargs)
    return Activity.objects.create(**defaults)


@pytest.mark.django_db
class TestActivityDownloadMarkdownView:
    def test_download_with_task_result(self, logged_in_client):
        tr = _create_task_result(return_value={"response": "# Security Report\n\nAll good.", "code_changes": False})
        activity = _create_activity(task_result=tr)

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 200
        assert response["Content-Type"] == "text/markdown; charset=utf-8"
        assert "attachment" in response["Content-Disposition"]
        assert ".md" in response["Content-Disposition"]

        body = response.content.decode()
        assert "---" in body
        assert "repository: group/project" in body
        assert "# Security Report" in body
        assert "All good." in body

    def test_download_with_result_summary_fallback(self, logged_in_client):
        activity = _create_activity(result_summary="Summary of the result")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 200
        body = response.content.decode()
        assert "Summary of the result" in body

    def test_download_includes_metadata(self, logged_in_client):
        activity = _create_activity(
            result_summary="Result content", ref="feature-branch", issue_iid=42, merge_request_iid=10
        )

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        body = response.content.decode()
        assert "ref: feature-branch" in body
        assert "issue: '#42'" in body
        assert "merge_request: '!10'" in body
        assert "trigger: Scheduled Run" in body

    def test_download_filename_format(self, logged_in_client):
        activity = _create_activity(result_summary="Content")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        disposition = response["Content-Disposition"]
        assert disposition.startswith('attachment; filename="daiv-group-project-')
        assert disposition.endswith('.md"')

    def test_task_result_response_takes_priority_over_result_summary(self, logged_in_client):
        tr = _create_task_result(return_value={"response": "Full response text", "code_changes": False})
        activity = _create_activity(task_result=tr, result_summary="Truncated summary")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        body = response.content.decode()
        assert "Full response text" in body
        assert "Truncated summary" not in body

    def test_legacy_return_value_without_response_falls_back_to_summary(self, logged_in_client):
        tr = _create_task_result(return_value={"code_changes": True})
        activity = _create_activity(task_result=tr, result_summary="Fallback summary")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        body = response.content.decode()
        assert "Fallback summary" in body

    def test_non_successful_activity_returns_404(self, logged_in_client):
        activity = _create_activity(status=ActivityStatus.FAILED, result_summary="error")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 404

    def test_successful_activity_without_result_returns_404(self, logged_in_client):
        activity = _create_activity(result_summary="")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 404

    def test_unauthenticated_redirects_to_login(self):
        activity = _create_activity(result_summary="Content")
        client = Client()

        response = client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 302
        assert "/accounts/login/" in response.url


@pytest.mark.django_db
class TestActivityListView:
    def test_unauthenticated_redirects_to_login(self):
        response = Client().get(reverse("activity_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_owner_scoping_applied_before_filters(self, logged_in_client, user):
        mine = _create_activity(user=user, repo_id="mine/repo")
        other = User.objects.create_user(
            username="bob",
            email="bob@test.com",
            password="testpass123",  # noqa: S106
        )
        theirs = _create_activity(user=other, repo_id="mine/repo")

        response = logged_in_client.get(reverse("activity_list"), {"repo": "mine/repo"})

        assert response.status_code == 200
        activities = list(response.context["activities"])
        assert mine in activities
        # by_owner must run before the filterset — without it, the repo filter would leak `theirs`.
        assert theirs not in activities

    def test_filter_by_status(self, logged_in_client, user):
        success = _create_activity(user=user, status=ActivityStatus.SUCCESSFUL)
        failed = _create_activity(user=user, status=ActivityStatus.FAILED)
        response = logged_in_client.get(reverse("activity_list"), {"status": ActivityStatus.SUCCESSFUL})
        activities = list(response.context["activities"])
        assert success in activities
        assert failed not in activities

    def test_date_param_names_are_date_from_and_date_to(self, logged_in_client, user):
        """Lock in the URL contract after the `from`/`to` → `date_from`/`date_to` rename."""
        _create_activity(user=user)
        response = logged_in_client.get(reverse("activity_list"), {"date_from": "2020-01-01", "date_to": "2100-01-01"})
        assert response.status_code == 200
        # Values round-trip to the template context so the date inputs stay populated.
        assert response.context["current_from"] == "2020-01-01"
        assert response.context["current_to"] == "2100-01-01"

    def test_invalid_filter_drops_silently(self, logged_in_client, user):
        activity = _create_activity(user=user)
        response = logged_in_client.get(reverse("activity_list"), {"status": "bogus"})
        assert response.status_code == 200
        # Invalid choice is dropped; full (owner-scoped) list is shown and context key is empty.
        assert activity in response.context["activities"]
        assert response.context["current_status"] == ""


@pytest.mark.django_db
class TestActivityDetailView:
    def _get(self, logged_in_client, activity):
        response = logged_in_client.get(reverse("activity_detail", kwargs={"pk": activity.pk}))
        assert response.status_code == 200
        return response

    def test_h1_uses_first_line_of_prompt(self, logged_in_client, user):
        activity = _create_activity(user=user, prompt="Refactor checkout\nMore context")
        body = self._get(logged_in_client, activity).content.decode()
        assert "<h1" in body
        assert "Refactor checkout" in body
        assert "Activity Detail" not in body

    def test_issue_webhook_without_prompt_titles_with_iid(self, logged_in_client, user):
        activity = _create_activity(user=user, trigger_type=TriggerType.ISSUE_WEBHOOK, prompt="", issue_iid=412)
        body = self._get(logged_in_client, activity).content.decode()
        assert "Issue #412" in body

    def test_status_strip_shows_retry_when_retryable(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.SUCCESSFUL)
        body = self._get(logged_in_client, activity).content.decode()
        assert reverse("runs:agent_run_new") + f"?from={activity.pk}" in body
        assert "Retry" in body

    def test_status_strip_hides_retry_for_webhook_activity(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.SUCCESSFUL, trigger_type=TriggerType.ISSUE_WEBHOOK)
        body = self._get(logged_in_client, activity).content.decode()
        assert f"?from={activity.pk}" not in body

    def test_status_strip_flags_pruned_when_task_result_missing(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.SUCCESSFUL, task_result=None)
        body = self._get(logged_in_client, activity).content.decode()
        assert "pruned" in body.lower()

    def test_successful_hero_renders_response_text(self, logged_in_client, user):
        tr = _create_task_result(return_value={"response": "# Done\nHere is the report.", "code_changes": False})
        activity = _create_activity(user=user, task_result=tr, status=ActivityStatus.SUCCESSFUL)
        body = self._get(logged_in_client, activity).content.decode()
        assert "Here is the report." in body
        assert "Copy" in body
        assert reverse("activity_download_md", kwargs={"pk": activity.pk}) in body

    def test_successful_hero_shows_open_mr_button_when_url_set(self, logged_in_client, user):
        tr = _create_task_result(return_value={"response": "Result body", "code_changes": True})
        activity = _create_activity(
            user=user,
            task_result=tr,
            status=ActivityStatus.SUCCESSFUL,
            merge_request_iid=1289,
            merge_request_web_url="https://gitlab.example.com/acme/web/-/merge_requests/1289",
        )
        body = self._get(logged_in_client, activity).content.decode()
        assert "https://gitlab.example.com/acme/web/-/merge_requests/1289" in body
        assert "!1289" in body

    def test_pruned_success_shows_notice_and_summary(self, logged_in_client, user):
        activity = _create_activity(
            user=user,
            status=ActivityStatus.SUCCESSFUL,
            task_result=None,
            result_summary="Appended release notes to CHANGELOG.md.",
        )
        body = self._get(logged_in_client, activity).content.decode()
        assert "pruned" in body.lower()
        assert "Appended release notes to CHANGELOG.md." in body
        assert reverse("activity_download_md", kwargs={"pk": activity.pk}) not in body

    def test_pruned_success_without_summary_shows_only_notice(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.SUCCESSFUL, task_result=None, result_summary="")
        body = self._get(logged_in_client, activity).content.decode()
        assert "pruned" in body.lower()

    def test_failed_hero_renders_exception_class_and_traceback(self, logged_in_client, user):
        tr = _create_task_result(status="FAILED")
        tr.exception_class_path = "automation.agent.errors.AgentExecutionError"
        tr.traceback = "Traceback (most recent call last):\n  File ..."
        tr.save(update_fields=["exception_class_path", "traceback"])
        activity = _create_activity(user=user, task_result=tr, status=ActivityStatus.FAILED)
        body = self._get(logged_in_client, activity).content.decode()
        assert "automation.agent.errors.AgentExecutionError" in body
        assert "Traceback (most recent call last):" in body
        assert reverse("activity_download_md", kwargs={"pk": activity.pk}) not in body

    def test_failed_hero_falls_back_to_error_message_when_pruned(self, logged_in_client, user):
        activity = _create_activity(
            user=user,
            status=ActivityStatus.FAILED,
            task_result=None,
            error_message="automation.agent.errors.AgentExecutionError\nDependency resolution failed",
        )
        body = self._get(logged_in_client, activity).content.decode()
        assert "Dependency resolution failed" in body

    def test_running_hero_shows_spinner_and_refresh_note(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.RUNNING)
        body = self._get(logged_in_client, activity).content.decode()
        assert "Agent is working" in body
        assert "refreshes automatically" in body
        assert 'role="status"' in body

    def test_pending_hero_uses_running_template(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.READY)
        body = self._get(logged_in_client, activity).content.decode()
        assert "Agent is working" in body

    def test_prompt_disclosure_is_details_element(self, logged_in_client, user):
        activity = _create_activity(user=user, prompt="Do the thing.")
        body = self._get(logged_in_client, activity).content.decode()
        assert "<details" in body
        assert "Do the thing." in body

    def test_prompt_disclosure_open_by_default_when_running(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.RUNNING, prompt="Still running prompt")
        body = self._get(logged_in_client, activity).content.decode()
        assert "<details open" in body or "<details  open" in body or "<details\nopen" in body

    def test_prompt_disclosure_collapsed_by_default_on_success(self, logged_in_client, user):
        tr = _create_task_result(return_value={"response": "Result", "code_changes": False})
        activity = _create_activity(user=user, task_result=tr, status=ActivityStatus.SUCCESSFUL, prompt="Prompt")
        body = self._get(logged_in_client, activity).content.decode()
        assert "<details" in body
        assert "<details open" not in body

    def test_prompt_disclosure_omitted_when_prompt_empty(self, logged_in_client, user):
        activity = _create_activity(user=user, prompt="")
        body = self._get(logged_in_client, activity).content.decode()
        assert "Copy prompt" not in body

    def test_rail_timing_shows_created_started_finished_when_terminal(self, logged_in_client, user):
        from django.utils import timezone

        now = timezone.now()
        activity = _create_activity(user=user, status=ActivityStatus.SUCCESSFUL, started_at=now, finished_at=now)
        body = self._get(logged_in_client, activity).content.decode()
        assert "Created" in body
        assert "Started" in body
        assert "Finished" in body

    def test_rail_timing_labels_failed_step_when_activity_failed(self, logged_in_client, user):
        from django.utils import timezone

        now = timezone.now()
        activity = _create_activity(user=user, status=ActivityStatus.FAILED, started_at=now, finished_at=now)
        body = self._get(logged_in_client, activity).content.decode()
        assert "Failed" in body
        assert ">Finished<" not in body

    def test_rail_context_renders_mr_link_when_url_present(self, logged_in_client, user):
        activity = _create_activity(
            user=user,
            merge_request_iid=1289,
            merge_request_web_url="https://gitlab.example.com/acme/web/-/merge_requests/1289",
        )
        body = self._get(logged_in_client, activity).content.decode()
        assert "!1289" in body
        assert "https://gitlab.example.com/acme/web/-/merge_requests/1289" in body

    def test_rail_context_shows_model_row_only_when_use_max(self, logged_in_client, user):
        default_model = _create_activity(user=user, use_max=False)
        max_model = _create_activity(user=user, use_max=True)

        body_default = self._get(logged_in_client, default_model).content.decode()
        body_max = self._get(logged_in_client, max_model).content.decode()
        assert ">Model<" not in body_default
        assert ">Max<" not in body_default
        assert ">Model<" in body_max
        assert ">Max<" in body_max

    def test_rail_context_hides_owner_for_non_admin(self, logged_in_client, user):
        activity = _create_activity(user=user)
        body = self._get(logged_in_client, activity).content.decode()
        assert ">Owner<" not in body

    def test_rail_usage_renders_token_and_cost_stats(self, logged_in_client, user):
        from decimal import Decimal

        activity = _create_activity(
            user=user, input_tokens=38200, output_tokens=3900, total_tokens=42100, cost_usd=Decimal("0.18")
        )
        body = self._get(logged_in_client, activity).content.decode()
        assert "42.1k" in body
        assert "$0.18" in body

    def test_rail_usage_shows_placeholders_when_no_data(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.RUNNING)
        body = self._get(logged_in_client, activity).content.decode()
        assert "—" in body

    def test_failed_hero_shows_no_details_when_nothing_available(self, logged_in_client, user):
        activity = _create_activity(user=user, status=ActivityStatus.FAILED, task_result=None, error_message="")
        body = self._get(logged_in_client, activity).content.decode()
        assert "No error details available." in body

    def test_rail_usage_per_model_breakdown_shown_when_multiple_models(self, logged_in_client, user):
        activity = _create_activity(
            user=user,
            total_tokens=100,
            usage_by_model={
                "claude-sonnet": {"input_tokens": 60, "output_tokens": 30, "cost_usd": "0.10"},
                "claude-haiku": {"input_tokens": 6, "output_tokens": 4, "cost_usd": "0.01"},
            },
        )
        body = self._get(logged_in_client, activity).content.decode()
        assert "Per-model breakdown" in body
        assert "claude-sonnet" in body
        assert "claude-haiku" in body

    def test_rail_usage_per_model_breakdown_hidden_for_single_model(self, logged_in_client, user):
        activity = _create_activity(
            user=user,
            total_tokens=100,
            usage_by_model={"claude-sonnet": {"input_tokens": 60, "output_tokens": 30, "cost_usd": "0.10"}},
        )
        body = self._get(logged_in_client, activity).content.decode()
        assert "Per-model breakdown" not in body


@pytest.mark.django_db
class TestActivityVisibilityForSubscribers:
    def _schedule(self, owner, **overrides):
        data = {
            "user": owner,
            "name": "s",
            "prompt": "p",
            "repos": [{"repo_id": "x/y", "ref": ""}],
            "frequency": Frequency.DAILY,
            "time": "12:00",
        }
        data.update(overrides)
        return ScheduledJob.objects.create(**data)

    def _activity(self, schedule, **overrides):
        data = {
            "trigger_type": TriggerType.SCHEDULE,
            "repo_id": schedule.repos[0]["repo_id"],
            "status": ActivityStatus.SUCCESSFUL,
            "scheduled_job": schedule,
            "user": schedule.user,
        }
        data.update(overrides)
        return Activity.objects.create(**data)

    def test_subscriber_can_view_linked_activity_detail(self, member_user):
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        schedule.subscribers.add(member_user)
        activity = self._activity(schedule)

        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.status_code == 200

    def test_non_subscriber_cannot_view_linked_activity_detail(self, member_user):
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        activity = self._activity(schedule)

        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.status_code == 404

    def test_subscriber_sees_activity_in_list(self, member_user):
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        schedule.subscribers.add(member_user)
        activity = self._activity(schedule)

        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("activity_list"))
        assert response.status_code == 200
        assert str(activity.pk) in response.content.decode()

    def test_list_does_not_duplicate_rows_for_admins_matching_twice(self, admin_user):
        owner = User.objects.create_user(username="owner", email="owner@t.com", password="x")  # noqa: S106
        schedule = self._schedule(owner)
        schedule.subscribers.add(admin_user)
        activity = self._activity(schedule)

        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("activity_list"))
        # Count rows by the detail-url anchor (one per distinct row).
        detail_url = reverse("activity_detail", args=[activity.pk])
        assert response.content.decode().count(detail_url) == 1


@pytest.mark.django_db
class TestActivityDetailSubscriberContext:
    def _fixture(self):
        owner = User.objects.create_user(username="own", email="own@t.com", password="x")  # noqa: S106
        sub = User.objects.create_user(username="sub", email="sub@t.com", password="x")  # noqa: S106
        schedule = ScheduledJob.objects.create(
            user=owner,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
        )
        schedule.subscribers.add(sub)
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
            user=owner,
        )
        return owner, sub, schedule, activity

    def test_is_subscriber_true_for_subscriber(self):
        _, sub, _, activity = self._fixture()
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.context["is_subscriber"] is True

    def test_is_subscriber_false_for_owner(self):
        owner, _, _, activity = self._fixture()
        client = Client()
        client.force_login(owner)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        assert response.context["is_subscriber"] is False

    def test_unsubscribe_button_visible_to_subscriber(self):
        _, sub, schedule, activity = self._fixture()
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        assert reverse("schedule_unsubscribe", args=[schedule.pk]) in html
        assert "Unsubscribe" in html

    def test_unsubscribe_button_hidden_for_owner(self):
        owner, _, _, activity = self._fixture()
        client = Client()
        client.force_login(owner)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        assert "schedule_unsubscribe" not in html
        assert "Unsubscribe" not in html

    def test_schedule_name_is_plain_text_for_subscriber(self):
        _, sub, schedule, activity = self._fixture()
        client = Client()
        client.force_login(sub)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        assert reverse("schedule_update", args=[schedule.pk]) not in html
        assert schedule.name in html

    def test_schedule_name_is_link_for_owner(self):
        owner, _, schedule, activity = self._fixture()
        client = Client()
        client.force_login(owner)
        response = client.get(reverse("activity_detail", args=[activity.pk]))
        html = response.content.decode()
        assert reverse("schedule_update", args=[schedule.pk]) in html
