from django.urls import reverse

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from accounts.models import User


@pytest.fixture
def user(db) -> User:
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    return User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106


@pytest.fixture
def admin(db) -> User:
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    return User.objects.create_user(username="a", email="a@e.com", password="x", is_staff=True)  # noqa: S106


@pytest.mark.django_db
def test_list_shows_user_envs_and_globals(client, user):
    SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="x")
    SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="g", is_default=True)
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:list"))
    assert resp.status_code == 200
    assert b"dev" in resp.content
    assert b"Default" in resp.content


@pytest.mark.django_db
def test_non_admin_cannot_edit_global(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="g", is_default=True)
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_admin_can_edit_global(client, admin):
    env = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="g", is_default=True)
    client.force_login(admin)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))
    assert resp.status_code == 200


@pytest.mark.django_db
def test_user_cannot_see_other_users_envs(client, user, db):
    other = User.objects.create_user(username="other", email="o@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=other, name="dev", base_image="x")
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_delete_global_default_blocked(client, admin):
    env = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="g", is_default=True)
    client.force_login(admin)
    resp = client.post(reverse("sandbox_envs:delete", args=[env.id]))
    assert resp.status_code == 409
    assert SandboxEnvironment.objects.filter(pk=env.id).exists()


@pytest.mark.django_db
def test_edit_template_does_not_leak_secret_values(client, user):
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[{"name": "TOKEN", "value": "real-secret", "is_secret": True}],
    )
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))
    assert resp.status_code == 200
    assert b"real-secret" not in resp.content
