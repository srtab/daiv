import pytest
from sandbox_envs.models import SandboxEnvironment, Scope


@pytest.mark.django_db
def test_seed_migration_creates_single_global_default(monkeypatch):
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_base_image", "python:3.12-bookworm")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_cpu", 2.0)
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_memory", 4_000_000_000)
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_network_enabled", True)
    # Pretend none of the fields are env-locked, so all should be persisted on the row.
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: False)

    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    # Re-run the data migration's seed function directly (it's idempotent).
    from sandbox_envs.migrations._seed import seed_global_default

    seed_global_default(SandboxEnvironment)

    env = SandboxEnvironment.objects.get(scope=Scope.GLOBAL, is_default=True)
    assert env.name == "Default"
    assert env.base_image == "python:3.12-bookworm"
    assert env.cpus == 2
    assert env.memory_bytes == 4_000_000_000
    assert env.network_enabled is True

    # Idempotency: calling twice produces no duplicate row.
    seed_global_default(SandboxEnvironment)
    assert SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).count() == 1


@pytest.mark.django_db
def test_seed_migration_leaves_env_locked_fields_null(monkeypatch):
    """Env-locked fields stay None on the row; the runtime overlay reads them live."""
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_base_image", "from-env:latest")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_cpu", 8.0)
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_memory", None)
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_network_enabled", False)
    # Lock base_image and cpu via env var (simulated).
    monkeypatch.setattr(
        "core.site_settings.site_settings.is_env_locked", lambda name: name in {"sandbox_base_image", "sandbox_cpu"}
    )

    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    from sandbox_envs.migrations._seed import seed_global_default

    seed_global_default(SandboxEnvironment)

    env = SandboxEnvironment.objects.get(scope=Scope.GLOBAL, is_default=True)
    # Env-locked → not persisted; fallback to "python:3.12-alpine" when row would be empty.
    assert env.base_image == "python:3.12-alpine"
    assert env.cpus is None
    # Non-locked → persisted.
    assert env.network_enabled is False
