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
    assert form.fields["memory_value"].disabled is True
    assert form.fields["memory_unit"].disabled is True
    # 2_000_000_000 isn't a whole GiB, so we expect MiB rendering.
    assert form.initial["memory_value"] == 2_000_000_000 // (2**20)
    assert form.initial["memory_unit"] == "MiB"
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


@pytest.mark.django_db
def test_form_memory_value_and_unit_set_memory_bytes():
    from accounts.models import User

    user = User.objects.create_user(username="m1", email="m1@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_value": "1",
            "memory_unit": "GiB",
            "network_choice": "default",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    assert env.memory_bytes == 2**30


@pytest.mark.django_db
def test_form_empty_memory_value_maps_to_none():
    from accounts.models import User

    user = User.objects.create_user(username="m2", email="m2@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": "default",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    assert env.memory_bytes is None


@pytest.mark.django_db
@pytest.mark.parametrize(("choice", "expected"), [("default", None), ("on", True), ("off", False)])
def test_form_network_choice_maps_to_network_enabled(choice, expected):
    from accounts.models import User

    user = User.objects.create_user(username=f"n-{choice}", email=f"n-{choice}@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": choice,
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    assert env.network_enabled is expected


@pytest.mark.django_db
def test_form_prefills_memory_and_network_from_instance():
    from accounts.models import User

    user = User.objects.create_user(username="p1", email="p1@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=user, name="dev", base_image="alpine", memory_bytes=2 * 2**30, network_enabled=True
    )
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    assert form.initial["memory_value"] == 2
    assert form.initial["memory_unit"] == "GiB"
    assert form.initial["network_choice"] == "on"


@pytest.mark.django_db
def test_form_prefill_memory_in_mib_when_not_whole_gib():
    from accounts.models import User

    user = User.objects.create_user(username="p2", email="p2@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=user, name="dev", base_image="alpine", memory_bytes=512 * 2**20, network_enabled=None
    )
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    assert form.initial["memory_value"] == 512
    assert form.initial["memory_unit"] == "MiB"
    assert form.initial["network_choice"] == "default"


@pytest.mark.django_db
def test_admin_form_locks_network_field(monkeypatch):
    from accounts.models import User

    user = User.objects.create_user(username="al-net", email="al-net@e.com", password="x", is_staff=True)  # noqa: S106
    monkeypatch.setattr(
        "core.site_settings.site_settings.is_env_locked", lambda name: name == "sandbox_network_enabled"
    )
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_network_enabled", True)
    form = SandboxEnvironmentForm(user=user, is_admin=True, is_default_form=True)
    assert form.fields["network_choice"].disabled is True
    assert form.initial.get("network_choice") == "on"
    assert "DAIV_SANDBOX_NETWORK_ENABLED" in (form.fields["network_choice"].help_text or "")


@pytest.mark.django_db
def test_admin_form_locks_cpus_field(monkeypatch):
    from decimal import Decimal

    from accounts.models import User

    user = User.objects.create_user(username="al-cpu", email="al-cpu@e.com", password="x", is_staff=True)  # noqa: S106
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: name == "sandbox_cpu")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_cpu", Decimal("2.5"))
    form = SandboxEnvironmentForm(user=user, is_admin=True, is_default_form=True)
    assert form.fields["cpus"].disabled is True
    assert form.initial.get("cpus") == Decimal("2.5")


@pytest.mark.django_db
def test_admin_form_locks_memory_in_whole_gib(monkeypatch):
    from accounts.models import User

    user = User.objects.create_user(username="al-mem", email="al-mem@e.com", password="x", is_staff=True)  # noqa: S106
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: name == "sandbox_memory")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_memory", 4 * 2**30)
    form = SandboxEnvironmentForm(user=user, is_admin=True, is_default_form=True)
    assert form.fields["memory_value"].disabled is True
    assert form.fields["memory_unit"].disabled is True
    assert form.initial["memory_value"] == 4
    assert form.initial["memory_unit"] == "GiB"


@pytest.mark.django_db
def test_form_rejects_custom_memory_mode_without_value():
    from accounts.models import User

    user = User.objects.create_user(username="m3", email="m3@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_mode": "custom",
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": "default",
            "cpu_mode": "default",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert not form.is_valid()
    assert "memory_value" in form.errors


@pytest.mark.django_db
def test_form_rejects_custom_cpu_mode_without_value():
    from accounts.models import User

    user = User.objects.create_user(username="c1", email="c1@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_mode": "default",
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": "default",
            "cpu_mode": "custom",
            "cpus": "",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert not form.is_valid()
    assert "cpus" in form.errors
