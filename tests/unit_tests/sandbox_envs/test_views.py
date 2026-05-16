import json

from django.urls import reverse

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from accounts.models import Role, User


@pytest.fixture
def user(db) -> User:
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    return User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106


@pytest.fixture
def admin(db) -> User:
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    return User.objects.create_user(username="a", email="a@e.com", password="x", role=Role.ADMIN)  # noqa: S106


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
def test_set_default_htmx_returns_global_envs_fragment(client, admin):
    old = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Old", base_image="g", is_default=True)
    new = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="New", base_image="g")
    client.force_login(admin)

    resp = client.post(reverse("sandbox_envs:set_default", args=[new.id]), HTTP_HX_REQUEST="true")

    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")
    rendered = [t.name for t in resp.templates]
    assert "sandbox_envs/_global_envs.html" in rendered
    assert "base_app.html" not in rendered
    new.refresh_from_db()
    old.refresh_from_db()
    assert new.is_default
    assert not old.is_default


@pytest.mark.django_db
def test_set_default_rejects_non_admin(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="g", base_image="x")
    client.force_login(user)

    resp = client.post(reverse("sandbox_envs:set_default", args=[env.id]))

    assert resp.status_code == 403
    env.refresh_from_db()
    assert not env.is_default


@pytest.mark.django_db
def test_set_default_rejects_user_scope_env(client, admin):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=admin, name="dev", base_image="x")
    client.force_login(admin)

    resp = client.post(reverse("sandbox_envs:set_default", args=[env.id]))

    assert resp.status_code == 404


@pytest.mark.django_db
def test_user_cannot_delete_other_users_env(client, user):
    other = User.objects.create_user(username="other", email="o@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=other, name="dev", base_image="x")
    client.force_login(user)

    resp = client.post(reverse("sandbox_envs:delete", args=[env.id]))

    assert resp.status_code == 404
    assert SandboxEnvironment.objects.filter(pk=env.id).exists()


@pytest.mark.django_db
def test_set_default_non_htmx_redirects(client, admin):
    SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Old", base_image="g", is_default=True)
    new = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="New", base_image="g")
    client.force_login(admin)

    resp = client.post(reverse("sandbox_envs:set_default", args=[new.id]))

    assert resp.status_code == 302
    assert resp["Location"] == reverse("sandbox_envs:list")


@pytest.mark.django_db
def test_create_htmx_get_returns_form_body_fragment(client, user):
    client.force_login(user)

    resp = client.get(reverse("sandbox_envs:create"), HTTP_HX_REQUEST="true")

    assert resp.status_code == 200
    rendered = [t.name for t in resp.templates]
    assert "sandbox_envs/_form_body.html" in rendered
    assert "base_app.html" not in rendered
    assert b'hx-post="' in resp.content


@pytest.mark.django_db
def test_create_htmx_post_success_fires_env_created(client, user):
    client.force_login(user)

    resp = client.post(
        reverse("sandbox_envs:create"),
        data={
            "name": "drawer-env",
            "description": "",
            "scope": Scope.USER,
            "base_image": "alpine:latest",
            "network_choice": "default",
            "memory_value": "",
            "memory_unit": "MiB",
            "env_vars_json": "[]",
        },
        HTTP_HX_REQUEST="true",
    )

    assert resp.status_code == 204
    trigger = json.loads(resp.headers["HX-Trigger"])
    env = SandboxEnvironment.objects.get(name="drawer-env")
    payload = trigger["env-created"]
    assert payload == {
        "id": str(env.id),
        "name": "drawer-env",
        "scope": Scope.USER,
        "scope_display": env.get_scope_display(),
    }
    assert env.user == user


@pytest.mark.django_db
def test_create_htmx_post_invalid_returns_form_body_with_errors(client, user):
    client.force_login(user)

    resp = client.post(
        reverse("sandbox_envs:create"),
        data={"name": "", "base_image": "", "env_vars_json": "[]"},
        HTTP_HX_REQUEST="true",
    )

    assert resp.status_code == 200
    rendered = [t.name for t in resp.templates]
    assert "sandbox_envs/_form_body.html" in rendered
    assert "base_app.html" not in rendered
    assert "HX-Trigger" not in resp.headers
    assert resp.context["form"].errors
    assert b"text-red-400" in resp.content
    assert not SandboxEnvironment.objects.filter(scope=Scope.USER, user=user).exists()


@pytest.mark.django_db
def test_edit_page_shows_delete_link(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="x")
    client.force_login(user)

    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))

    assert resp.status_code == 200
    assert reverse("sandbox_envs:delete", args=[env.id]).encode() in resp.content


@pytest.mark.django_db
def test_create_page_does_not_show_delete_link(client, user):
    client.force_login(user)

    resp = client.get(reverse("sandbox_envs:create"))

    assert resp.status_code == 200
    assert b"btn-danger-outline" not in resp.content


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


@pytest.mark.django_db
def test_create_context_includes_global_default_summary(client, user):
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:create"))
    assert resp.status_code == 200
    summary = resp.context["global_default_summary"]
    assert set(summary.keys()) == {"network", "memory", "cpus", "has_network", "has_memory", "has_cpus"}


@pytest.mark.django_db
def test_edit_htmx_get_returns_form_body_fragment(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine")
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]), HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    rendered = [t.name for t in resp.templates]
    assert "sandbox_envs/_form_body.html" in rendered
    assert "base_app.html" not in rendered


@pytest.mark.django_db
def test_edit_htmx_post_success_fires_env_updated(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine")
    client.force_login(user)
    resp = client.post(
        reverse("sandbox_envs:edit", args=[env.id]),
        data={
            "name": "dev-renamed",
            "description": "",
            "scope": Scope.USER,
            "base_image": "alpine:latest",
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": "default",
            "env_vars_json": "[]",
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 204
    trigger = json.loads(resp.headers["HX-Trigger"])
    env.refresh_from_db()
    assert trigger["env-updated"] == {
        "id": str(env.id),
        "name": "dev-renamed",
        "scope": Scope.USER,
        "scope_display": env.get_scope_display(),
    }


@pytest.mark.django_db
def test_edit_htmx_post_invalid_returns_form_body_with_errors(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine")
    client.force_login(user)
    resp = client.post(
        reverse("sandbox_envs:edit", args=[env.id]),
        data={"name": "", "base_image": "", "env_vars_json": "[]"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    rendered = [t.name for t in resp.templates]
    assert "sandbox_envs/_form_body.html" in rendered
    assert "HX-Trigger" not in resp.headers
    assert resp.context["form"].errors


@pytest.mark.django_db
def test_edit_global_default_sets_is_default_form_true(client, admin):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    env = SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
    )
    client.force_login(admin)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))
    assert resp.status_code == 200
    assert resp.context["is_default_form"] is True


@pytest.mark.django_db
def test_edit_non_default_user_env_sets_is_default_form_false(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine")
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))
    assert resp.status_code == 200
    assert resp.context["is_default_form"] is False


@pytest.mark.django_db
def test_create_context_sets_is_default_form_false(client, user):
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:create"))
    assert resp.status_code == 200
    assert resp.context["is_default_form"] is False


@pytest.mark.django_db
def test_invalid_create_post_preserves_submitted_env_vars(client, user):
    client.force_login(user)
    submitted_json = '[{"name": "API_KEY", "value": "sk-typed", "is_secret": false}]'
    resp = client.post(
        reverse("sandbox_envs:create"),
        data={
            "name": "",
            "base_image": "alpine",
            "scope": Scope.USER,
            "network_choice": "default",
            "memory_value": "",
            "memory_unit": "MiB",
            "env_vars_json": submitted_json,
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    assert resp.context["env_vars_initial"] == submitted_json


@pytest.mark.django_db
def test_edit_renders_existing_env_vars_with_escaped_quotes(client, user):
    """Regression: previously rendered with ``|safe``, so the JSON's literal ``"``
    closed the ``x-data`` attribute and Alpine got an empty rows list. The escaped
    form (``&quot;``) is what the browser decodes back to ``"`` *inside* the
    attribute value, leaving Alpine to parse the JSON correctly."""
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine",
        env_vars=[{"name": "API_KEY", "value": "plain-value", "is_secret": False}],
    )
    client.force_login(user)
    resp = client.get(reverse("sandbox_envs:edit", args=[env.id]))
    assert resp.status_code == 200
    body = resp.content.decode()
    # The unescaped JSON ending the attribute early is the bug we're guarding
    # against — any ``[{"name"`` substring would have closed ``x-data="…"``.
    assert '[{"name"' not in body
    assert "&quot;name&quot;" in body
    assert "&quot;API_KEY&quot;" in body


@pytest.mark.django_db
def test_invalid_edit_post_preserves_submitted_env_vars(client, user):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine")
    client.force_login(user)
    submitted_json = '[{"name": "ABC", "value": "v", "is_secret": false}]'
    resp = client.post(
        reverse("sandbox_envs:edit", args=[env.id]),
        data={
            "name": "",
            "base_image": "alpine",
            "scope": Scope.USER,
            "network_choice": "default",
            "memory_value": "",
            "memory_unit": "MiB",
            "env_vars_json": submitted_json,
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    assert resp.context["env_vars_initial"] == submitted_json
