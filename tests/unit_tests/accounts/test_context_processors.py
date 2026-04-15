from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory
from django.urls import reverse

import pytest
from activity.models import Activity, ActivityStatus, TriggerType

from accounts.context_processors import nav
from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="x123456789")  # noqa: S106


@pytest.mark.django_db
class TestNavContextProcessor:
    def test_returns_empty_for_anonymous_user(self):
        request = RequestFactory().get("/dashboard/")
        request.user = AnonymousUser()
        assert nav(request) == {}

    def test_returns_zero_running_jobs_when_none(self, user):
        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 0
        assert out["nav_active_section"] == ""

    def test_counts_only_running_jobs_owned_by_user(self, user, db):
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=user, repo_id="daiv/api"
        )
        Activity.objects.create(
            status=ActivityStatus.SUCCESSFUL, trigger_type=TriggerType.MCP_JOB, user=user, repo_id="daiv/api"
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=other, repo_id="daiv/api"
        )

        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 1

    def test_resolves_active_section_from_url_name(self, db):
        user_obj = User.objects.create_user(username="charlie", email="c@test.com", password="x123456789")  # noqa: S106
        client = Client()
        client.force_login(user_obj)
        response = client.get(reverse("dashboard"))
        assert response.context["nav_active_section"] == "dashboard"
