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


@pytest.mark.django_db
def test_patch_preserves_secret_on_empty_string_submission(auth_pair):
    """Submitting ``value=""`` for an existing secret keeps the stored value
    (UI uses an empty input to mean "no change")."""
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
        data=json.dumps({"env_vars": [{"name": "TOKEN", "value": "", "is_secret": True}]}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 200
    env.refresh_from_db()
    assert env.env_vars == [{"name": "TOKEN", "value": "real-secret", "is_secret": True}]


@pytest.mark.django_db
def test_patch_overwrites_secret_when_new_value_supplied(auth_pair):
    """A non-empty, non-mask secret value must overwrite the stored value, not
    be treated as preserve-existing."""
    user, raw = auth_pair
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[{"name": "TOKEN", "value": "old-secret", "is_secret": True}],
    )
    c = Client()
    resp = c.patch(
        f"/api/sandbox-envs/{env.id}",
        data=json.dumps({"env_vars": [{"name": "TOKEN", "value": "new-secret", "is_secret": True}]}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 200
    env.refresh_from_db()
    assert env.env_vars == [{"name": "TOKEN", "value": "new-secret", "is_secret": True}]


@pytest.mark.django_db
def test_patch_removes_secret_when_dropped_from_payload(auth_pair):
    """Omitting a row from ``env_vars`` must remove it — preservation cannot
    resurrect a deleted secret by name."""
    user, raw = auth_pair
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[
            {"name": "TOKEN", "value": "stays", "is_secret": True},
            {"name": "GONE", "value": "removed", "is_secret": True},
        ],
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
    assert env.env_vars == [{"name": "TOKEN", "value": "stays", "is_secret": True}]


@pytest.mark.django_db
def test_user_cannot_get_other_users_env_via_update(auth_pair):
    """USER-A must not be able to PATCH USER-B's env (returns 404, not 200/403)."""
    user, raw = auth_pair
    other = User.objects.create_user(username="other", email="o@e.com", password="x")  # noqa: S106
    other_env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=other, name="other-dev", base_image="alpine:latest"
    )
    c = Client()
    resp = c.patch(
        f"/api/sandbox-envs/{other_env.id}",
        data=json.dumps({"name": "stolen"}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 404
    other_env.refresh_from_db()
    assert other_env.name == "other-dev"


@pytest.mark.django_db
def test_user_cannot_delete_other_users_env(auth_pair):
    user, raw = auth_pair
    other = User.objects.create_user(username="other", email="o@e.com", password="x")  # noqa: S106
    other_env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=other, name="other-dev", base_image="alpine:latest"
    )
    c = Client()
    resp = c.delete(f"/api/sandbox-envs/{other_env.id}", **_bearer(raw))
    assert resp.status_code == 404
    assert SandboxEnvironment.objects.filter(pk=other_env.id).exists()


@pytest.mark.django_db
def test_list_excludes_other_users_envs(auth_pair):
    user, raw = auth_pair
    other = User.objects.create_user(username="other", email="o@e.com", password="x")  # noqa: S106
    SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="mine", base_image="x")
    SandboxEnvironment.objects.create(scope=Scope.USER, user=other, name="theirs", base_image="x")
    c = Client()
    resp = c.get("/api/sandbox-envs/", **_bearer(raw))
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()}
    assert "mine" in names
    assert "theirs" not in names


@pytest.mark.django_db
def test_non_admin_cannot_set_default(auth_pair):
    user, raw = auth_pair
    env = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Candidate", base_image="g")
    c = Client()
    resp = c.post(f"/api/sandbox-envs/{env.id}/set-default", **_bearer(raw))
    assert resp.status_code == 403
    env.refresh_from_db()
    assert env.is_default is False


@pytest.mark.django_db
def test_set_default_on_user_env_returns_404(auth_pair):
    user, raw = auth_pair
    user.is_staff = True
    user.save()
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="x")
    c = Client()
    resp = c.post(f"/api/sandbox-envs/{env.id}/set-default", **_bearer(raw))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_admin_create_with_is_default_promotes_atomically(auth_pair):
    """Creating a GLOBAL env with ``is_default=True`` must demote any existing
    default and succeed — not raise the partial unique constraint as a 500."""
    user, raw = auth_pair
    user.is_staff = True
    user.save()
    existing = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="OldDefault", base_image="g", is_default=True)
    c = Client()
    resp = c.post(
        "/api/sandbox-envs/",
        data=json.dumps({"name": "NewDefault", "scope": "global", "base_image": "alpine:latest", "is_default": True}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 201
    existing.refresh_from_db()
    assert existing.is_default is False
    new = SandboxEnvironment.objects.get(name="NewDefault")
    assert new.is_default is True


@pytest.mark.django_db
def test_create_invalid_payload_returns_400(auth_pair):
    """A model-validation error (e.g. blank base_image) must surface as 400,
    not 500."""
    user, raw = auth_pair
    c = Client()
    resp = c.post(
        "/api/sandbox-envs/",
        data=json.dumps({"name": "dev", "scope": "user", "base_image": "   "}),
        content_type="application/json",
        **_bearer(raw),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" in body or "errors" in body
