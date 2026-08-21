import json
from datetime import timedelta

from django.utils import timezone

import pytest
from mcp_servers.models import MCPServer

from accounts.models import User
from core.models import Provider, ProviderType
from schedules.forms import ScheduledJobCreateForm, ScheduledJobUpdateForm, ScheduleTemplateForm
from schedules.models import Frequency, Intent
from tests.unit_tests.mcp_servers.helpers import only_servers


@pytest.fixture
def openrouter_provider(db):
    Provider.objects.filter(slug="openrouter").delete()
    return Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=True
    )


def _valid_data(**overrides):
    data = {
        "name": "s",
        "prompt": "p",
        "repos": json.dumps([{"repo_id": "x/y", "ref": ""}]),
        "frequency": "daily",
        "cron_expression": "",
        "time": "12:00",
        "intent": Intent.WATCH_FIND,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestScheduledJobCreateForm:
    def test_valid_single_repo(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(), owner=member_user)
        assert form.is_valid(), form.errors
        assert form.cleaned_data["repos"] == [{"repo_id": "x/y", "ref": ""}]

    def test_valid_multi_repo(self, member_user):
        form = ScheduledJobCreateForm(
            data=_valid_data(repos=json.dumps([{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}])),
            owner=member_user,
        )
        assert form.is_valid(), form.errors

    def test_valid_with_muted(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(muted="on"), owner=member_user)
        assert form.is_valid(), form.errors

    def test_save_persists_repos(self, member_user):
        form = ScheduledJobCreateForm(
            data=_valid_data(repos=json.dumps([{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}])),
            owner=member_user,
        )
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        job.refresh_from_db()
        assert job.repos == [{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}]

    def test_rejects_empty_repos(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(repos="[]"), owner=member_user)
        assert not form.is_valid()
        assert "repos" in form.errors


@pytest.mark.django_db
class TestScheduledJobCreateFormSubscribers:
    def _sub_user(self, username="alice"):
        return User.objects.create_user(
            username=username,
            email=f"{username}@t.com",
            password="x",  # noqa: S106
        )

    def test_form_accepts_subscribers(self, member_user):
        alice = self._sub_user("alice")
        form = ScheduledJobCreateForm(data=_valid_data(subscribers=[alice.pk]), owner=member_user)
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        assert list(job.subscribers.all()) == [alice]

    def test_owner_excluded_from_queryset(self, member_user):
        form = ScheduledJobCreateForm(owner=member_user)
        qs_pks = list(form.fields["subscribers"].queryset.values_list("pk", flat=True))
        assert member_user.pk not in qs_pks

    def test_inactive_users_excluded_from_queryset(self, member_user):
        inactive = self._sub_user("bob")
        inactive.is_active = False
        inactive.save()
        form = ScheduledJobCreateForm(owner=member_user)
        qs_pks = list(form.fields["subscribers"].queryset.values_list("pk", flat=True))
        assert inactive.pk not in qs_pks

    def test_submitting_owner_pk_in_subscribers_is_rejected(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(subscribers=[member_user.pk]), owner=member_user)
        assert not form.is_valid()
        assert "subscribers" in form.errors

    def test_accepts_empty_subscribers(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(), owner=member_user)
        assert form.is_valid(), form.errors


def _once_data(_run_at, **overrides):
    data = {
        "name": "one-off",
        "prompt": "p",
        "repos": json.dumps([{"repo_id": "x/y", "ref": ""}]),
        "frequency": Frequency.ONCE,
        "cron_expression": "",
        "time": "",
        "run_at": _run_at.strftime("%Y-%m-%dT%H:%M"),
        "intent": Intent.WATCH_FIND,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestScheduledJobCreateFormOnce:
    def test_once_with_future_run_at_is_valid(self, member_user):
        future = timezone.now() + timedelta(hours=1)
        form = ScheduledJobCreateForm(data=_once_data(future), owner=member_user)
        assert form.is_valid(), form.errors

    def test_once_clears_irrelevant_fields(self, member_user):
        """Switching to ONCE in the UI should not leak stale cron/time values into the model."""
        future = timezone.now() + timedelta(hours=1)
        form = ScheduledJobCreateForm(
            data=_once_data(future, cron_expression="0 9 * * *", time="09:00"), owner=member_user
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["cron_expression"] == ""
        assert form.cleaned_data["time"] is None

    def test_non_once_clears_run_at(self, member_user):
        """Switching away from ONCE should not leak a stale run_at."""
        future = timezone.now() + timedelta(hours=1)
        data = _valid_data(run_at=future.strftime("%Y-%m-%dT%H:%M"))
        form = ScheduledJobCreateForm(data=data, owner=member_user)
        assert form.is_valid(), form.errors
        assert form.cleaned_data.get("run_at") is None

    def test_once_without_run_at_is_invalid(self, member_user):
        future = timezone.now() + timedelta(hours=1)
        form = ScheduledJobCreateForm(data=_once_data(future, run_at=""), owner=member_user)
        assert not form.is_valid()
        assert "run_at" in form.errors


@pytest.mark.django_db
class TestScheduledJobCreateFormAgentOverride:
    """``agent_model`` / ``agent_thinking_level`` validate via ``validate_agent_override``."""

    def test_form_accepts_valid_pair(self, openrouter_provider, member_user):
        form = ScheduledJobCreateForm(
            data=_valid_data(agent_model="openrouter:anthropic/claude-haiku-4.5", agent_thinking_level="low"),
            owner=member_user,
            user=member_user,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
        assert form.cleaned_data["agent_thinking_level"] == "low"

    def test_form_rejects_unknown_provider(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(agent_model="bogus:nope"), owner=member_user, user=member_user)
        assert not form.is_valid()
        assert "agent_model" in form.errors

    def test_form_accepts_empty_override(self, member_user):
        # Both fields empty: validator returns ("", "") without touching Provider.
        form = ScheduledJobCreateForm(data=_valid_data(), owner=member_user, user=member_user)
        assert form.is_valid(), form.errors
        assert form.cleaned_data["agent_model"] == ""
        assert form.cleaned_data["agent_thinking_level"] == ""

    def test_form_save_persists_override(self, openrouter_provider, member_user):
        form = ScheduledJobCreateForm(
            data=_valid_data(agent_model="openrouter:anthropic/claude-haiku-4.5", agent_thinking_level="high"),
            owner=member_user,
            user=member_user,
        )
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        job.refresh_from_db()
        assert job.agent_model == "openrouter:anthropic/claude-haiku-4.5"
        assert job.agent_thinking_level == "high"


def _valid_template_data(**overrides):
    data = {
        "name": "Nightly scan",
        "prompt": "p",
        "repos": json.dumps([{"repo_id": "x/y", "ref": ""}]),
        "frequency": "daily",
        "cron_expression": "",
        "time": "12:00",
        "intent": Intent.WATCH_FIND,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestScheduledJobFormIntent:
    def test_create_form_accepts_and_persists_intent(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(intent=Intent.DO_CHANGE), owner=member_user)
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        job.refresh_from_db()
        assert job.intent == Intent.DO_CHANGE

    def test_create_form_rejects_invalid_intent(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(intent="bogus"), owner=member_user)
        assert not form.is_valid()
        assert "intent" in form.errors

    def test_update_form_accepts_and_persists_intent(self, member_user):
        form = ScheduledJobUpdateForm(data=_valid_data(intent=Intent.REPORT), owner=member_user)
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        job.refresh_from_db()
        assert job.intent == Intent.REPORT

    def test_update_form_rejects_invalid_intent(self, member_user):
        form = ScheduledJobUpdateForm(data=_valid_data(intent="bogus"), owner=member_user)
        assert not form.is_valid()
        assert "intent" in form.errors

    def test_update_form_without_the_picker_keeps_the_stored_overrides(self, member_user):
        """Editing a schedule from a page whose picker never rendered must leave its MCP
        selection intact rather than silently rewriting it to "everything off"."""
        only_servers(("a", MCPServer.Status.ON_DEMAND))
        job = ScheduledJobCreateForm(data=_valid_data(mcp_servers='["a"]'), owner=member_user).save(commit=False)
        job.user = member_user
        job.save()
        assert job.mcp_overrides == {"a": "on"}

        form = ScheduledJobUpdateForm(data=_valid_data(), instance=job, owner=member_user)
        assert form.is_valid(), form.errors
        assert form.cleaned_data["mcp_overrides"] == {"a": "on"}


@pytest.mark.django_db
class TestScheduleTemplateFormIntent:
    def test_template_form_accepts_and_persists_intent(self):
        form = ScheduleTemplateForm(data=_valid_template_data(intent=Intent.REPORT))
        assert form.is_valid(), form.errors
        tpl = form.save()
        tpl.refresh_from_db()
        assert tpl.intent == Intent.REPORT

    def test_template_form_rejects_invalid_intent(self):
        form = ScheduleTemplateForm(data=_valid_template_data(intent="bogus"))
        assert not form.is_valid()
        assert "intent" in form.errors


@pytest.mark.django_db
def test_schedule_form_has_muted_not_notify_on(member_user):
    from schedules.forms import ScheduledJobCreateForm

    form = ScheduledJobCreateForm(owner=member_user, user=member_user)
    assert "muted" in form.fields
    assert "notify_on" not in form.fields


@pytest.mark.django_db
def test_schedule_form_persists_muted(member_user):
    from schedules.forms import ScheduledJobCreateForm

    form = ScheduledJobCreateForm(
        data={
            "name": "n",
            "prompt": "p",
            "repos": '[{"repo_id": "x/y", "ref": ""}]',
            "frequency": "daily",
            "time": "12:00",
            "intent": "watch-find",
            "muted": "on",
        },
        owner=member_user,
        user=member_user,
    )
    assert form.is_valid(), form.errors
    schedule = form.save(commit=False)
    schedule.user = member_user
    schedule.save()
    schedule.refresh_from_db()
    assert schedule.muted is True


def test_schedule_form_pool_uses_owner_not_editing_admin(member_user, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    # A USER server owned by member_user (only visible in member_user's pool).
    MCPServer.objects.create(
        name="mine",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="http://mine",
        status=MCPServer.Status.ON_DEMAND,
    )
    form = ScheduledJobCreateForm(owner=member_user, user=admin_user)
    names = {e.name for e in form.mcp_pool}
    assert "mine" in names  # owner's server, though the editor is the admin
