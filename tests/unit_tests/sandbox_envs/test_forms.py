import pytest
from sandbox_envs.forms import SandboxEnvironmentForm
from sandbox_envs.models import SandboxEnvironment, Scope


@pytest.mark.django_db
def test_user_form_rejects_global_scope_for_non_admin():
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={"name": "dev", "base_image": "alpine", "scope": Scope.GLOBAL}, user=user, is_admin=False
    )
    assert not form.is_valid()
    assert "scope" in form.errors


@pytest.mark.django_db
def test_admin_form_allows_global_and_locks_env_locked_fields(monkeypatch):
    from accounts.models import User

    user = User.objects.create_user(username="a", email="a@e.com", password="x", is_staff=True)  # noqa: S106
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: name == "sandbox_memory")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_memory", 2_000_000_000)
    form = SandboxEnvironmentForm(user=user, is_admin=True, is_default_form=True)
    assert form.fields["memory_bytes"].disabled is True
    assert form.initial.get("memory_bytes") == 2_000_000_000
    # base_image is no longer lockable → editable.
    assert form.fields["base_image"].disabled is False
    assert form.fields["cpus"].disabled is False


@pytest.mark.django_db
def test_form_validates_env_vars_shape():
    from accounts.models import User

    user = User.objects.create_user(username="u2", email="u2@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "env_vars_json": '[{"name": "bad name", "value": "v", "is_secret": false}]',
        },
        user=user,
        is_admin=False,
    )
    assert not form.is_valid()
    assert "env_vars" in form.errors or "env_vars_json" in form.errors or "__all__" in form.errors


@pytest.mark.django_db
def test_form_preserves_unchanged_secret_on_edit(db):
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[{"name": "TOKEN", "value": "real-secret", "is_secret": True}],
    )
    form = SandboxEnvironmentForm(
        instance=env,
        data={
            "name": "dev",
            "base_image": "alpine:latest",
            "scope": Scope.USER,
            "env_vars_json": '[{"name": "TOKEN", "value": "", "is_secret": true}]',
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    saved = form.save()
    assert saved.env_vars == [{"name": "TOKEN", "value": "real-secret", "is_secret": True}]


@pytest.mark.django_db
def test_form_refuses_when_existing_secrets_cannot_be_decrypted(db):
    """If the stored ciphertext is unreadable, the form must fail validation
    rather than silently persist ``"******"`` (or empty) as the new secret value."""
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[{"name": "TOKEN", "value": "real-secret", "is_secret": True}],
    )
    # Corrupt the ciphertext to simulate key rotation / DB tamper.
    env._env_vars_encrypted = "not-a-fernet-token"
    env.save(update_fields=["_env_vars_encrypted"])

    form = SandboxEnvironmentForm(
        instance=env,
        data={
            "name": "dev",
            "base_image": "alpine:latest",
            "scope": Scope.USER,
            "env_vars_json": '[{"name": "TOKEN", "value": "", "is_secret": true}]',
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid() is False
    # Original ciphertext is unchanged.
    env.refresh_from_db()
    assert env._env_vars_encrypted == "not-a-fernet-token"
