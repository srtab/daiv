from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import Role, User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="x123456789",  # noqa: S106
        role=Role.ADMIN,
    )


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestBreadcrumbs:
    def test_activity_list_has_no_breadcrumb(self, admin):
        response = _client(admin).get(reverse("activity_list"))
        assert b'data-testid="app-breadcrumb"' not in response.content

    def test_schedule_create_breadcrumb(self, admin):
        response = _client(admin).get(reverse("schedule_create"))
        assert b'data-testid="app-breadcrumb"' in response.content
        assert b"Schedules" in response.content
        assert b"New schedule" in response.content

    def test_user_create_breadcrumb(self, admin):
        response = _client(admin).get(reverse("user_create"))
        assert b'data-testid="app-breadcrumb"' in response.content
        assert b"Users" in response.content
        assert b"New user" in response.content
