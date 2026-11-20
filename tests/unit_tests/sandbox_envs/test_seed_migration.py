from importlib import import_module

from django.apps import apps

import pytest
from sandbox_envs.models import SandboxEnvironment, Visibility

image_migration = import_module("sandbox_envs.migrations.0007_default_image_with_git")


@pytest.mark.django_db
def test_seed_migration_creates_default_row_with_fallback_image():
    """The migration seeds a single GLOBAL Default row with the hardcoded fallback image."""
    SandboxEnvironment.objects.filter(scope=Visibility.GLOBAL).delete()
    from sandbox_envs.migrations._seed import seed_global_default

    seed_global_default(SandboxEnvironment)

    env = SandboxEnvironment.objects.get(scope=Visibility.GLOBAL, is_default=True)
    assert env.name == "Default"
    assert env.base_image == "ghcr.io/astral-sh/uv:python3.14-bookworm"

    # Idempotency: calling twice produces no duplicate row.
    seed_global_default(SandboxEnvironment)
    assert SandboxEnvironment.objects.filter(scope=Visibility.GLOBAL).count() == 1


@pytest.mark.django_db
def test_image_migration_replaces_the_untouched_gitless_default():
    SandboxEnvironment.objects.filter(scope=Visibility.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Visibility.GLOBAL, name="Default", base_image="python:3.12-alpine", is_default=True
    )

    image_migration.forwards(apps, None)

    env = SandboxEnvironment.objects.get(scope=Visibility.GLOBAL, name="Default")
    assert env.base_image == "ghcr.io/astral-sh/uv:python3.14-bookworm"


@pytest.mark.django_db
def test_image_migration_keeps_an_edited_default():
    SandboxEnvironment.objects.filter(scope=Visibility.GLOBAL).delete()
    SandboxEnvironment.objects.create(scope=Visibility.GLOBAL, name="Default", base_image="node:22", is_default=True)

    image_migration.forwards(apps, None)

    assert SandboxEnvironment.objects.get(scope=Visibility.GLOBAL, name="Default").base_image == "node:22"
