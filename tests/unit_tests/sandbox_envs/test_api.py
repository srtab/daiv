import json

from django.test import Client

import pytest
from asgiref.sync import async_to_sync
from sandbox_envs.models import SandboxEnvironment, Scope

from accounts.models import APIKey, User


@pytest.fixture
def auth_pair(db):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    api_key, raw = async_to_sync(APIKey.objects.create_key)(user=user, name="t", expires_at=None)
    return user, raw


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


@pytest.mark.django_db
def test_list_returns_user_and_global_envs(auth_pair):
    user, raw = auth_pair
    SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="x")
    SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="GlobalExtra", base_image="g")
    c = Client()
    resp = c.get("/api/sandbox-envs/", **_bearer(raw))
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()}
    assert "dev" in names
    assert "GlobalExtra" in names


@pytest.mark.django_db
def test_create_user_env(auth_pair):
    user, raw = auth_pair
    c = Client()
    resp = c.post(
        "/api/sandbox-envs/",
        data=json.dumps({"name": "dev", "scope": "user", "base_image": "alpine:latest"}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 201
    assert SandboxEnvironment.objects.filter(scope=Scope.USER, user=user, name="dev").exists()


@pytest.mark.django_db
def test_non_admin_cannot_create_global(auth_pair):
    user, raw = auth_pair
    c = Client()
    resp = c.post(
        "/api/sandbox-envs/",
        data=json.dumps({"name": "g", "scope": "global", "base_image": "alpine:latest"}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_cannot_delete_global_default(auth_pair):
    user, raw = auth_pair
    user.is_staff = True
    user.save()
    env = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="MyDefault", base_image="g", is_default=True)
    c = Client()
    resp = c.delete(f"/api/sandbox-envs/{env.id}", **_bearer(raw))
    assert resp.status_code == 409


@pytest.mark.django_db
def test_patch_preserves_unchanged_secrets(auth_pair):
    user, raw = auth_pair
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[{"name": "TOKEN", "value": "real-secret", "is_secret": True}],
    )
    c = Client()
    resp = c.patch(
        f"/api/sandbox-envs/{env.id}",
        data=json.dumps({"env_vars": [{"name": "TOKEN", "value": "******", "is_secret": True}]}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 200
    env.refresh_from_db()
    assert env.env_vars == [{"name": "TOKEN", "value": "real-secret", "is_secret": True}]
