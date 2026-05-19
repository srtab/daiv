import pytest
from sandbox_envs.models import SandboxEnvironment, Scope


@pytest.mark.django_db
def test_seed_migration_creates_default_row_with_fallback_image():
    """The migration seeds a single GLOBAL Default row with the hardcoded fallback image."""
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    from sandbox_envs.migrations._seed import seed_global_default

    seed_global_default(SandboxEnvironment)

    env = SandboxEnvironment.objects.get(scope=Scope.GLOBAL, is_default=True)
    assert env.name == "Default"
    assert env.base_image == "python:3.12-alpine"

    # Idempotency: calling twice produces no duplicate row.
    seed_global_default(SandboxEnvironment)
    assert SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).count() == 1
