from django.test import Client
from django.urls import reverse

import pytest


@pytest.mark.django_db
class TestAdminRequiredMixin:
    def test_admin_can_access(self, admin_user):
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("user_list"))
        assert response.status_code == 200

    def test_member_gets_403(self, member_user):
        client = Client()
        client.force_login(member_user)
        response = client.get(reverse("user_list"))
        assert response.status_code == 403

    def test_anonymous_redirected_to_login(self):
        client = Client()
        response = client.get(reverse("user_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url


@pytest.mark.django_db
class TestBreadcrumbs:
    def test_session_list_has_no_breadcrumb(self, admin_client):
        response = admin_client.get(reverse("session_list"))
        assert b'data-testid="app-breadcrumb"' not in response.content

    def test_chat_has_no_breadcrumb(self, admin_client):
        """The chat page is the composer plus the transcript; "Sessions › New" only
        pushes both down, and the sidebar already links back to the list."""
        response = admin_client.get(reverse("session_new_chat"))
        assert b'data-testid="app-breadcrumb"' not in response.content

    def test_schedule_create_breadcrumb(self, admin_client):
        response = admin_client.get(reverse("schedule_create"))
        assert b'data-testid="app-breadcrumb"' in response.content
        assert b"Schedules" in response.content
        assert b"New schedule" in response.content

    def test_user_create_breadcrumb(self, admin_client):
        response = admin_client.get(reverse("user_create"))
        assert b'data-testid="app-breadcrumb"' in response.content
        assert b"Users" in response.content
        assert b"New user" in response.content
