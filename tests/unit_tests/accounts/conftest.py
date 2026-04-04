from django.test import Client

import pytest

from accounts.models import Role, User


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="testpass123",  # noqa: S106
        role=Role.ADMIN,
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member",
        email="member@test.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture
def member_client(member_user):
    client = Client()
    client.force_login(member_user)
    return client
