from decimal import Decimal

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.services import SandboxEnvOverride, get_global_default, resolve_sandbox_env

from accounts.models import User


@pytest.mark.asyncio
async def test_resolve_returns_none_for_none_id():
    assert await resolve_sandbox_env(None) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_returns_none_for_missing():
    import uuid

    assert await resolve_sandbox_env(str(uuid.uuid4())) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_returns_override_with_decrypted_env_vars():
    user = await User.objects.acreate_user(username="alice", email="alice@example.com", password="x")  # noqa: S106
    env = await SandboxEnvironment.objects.acreate(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="python:3.12",
        memory_bytes=4_000_000_000,
        env_vars=[
            {"name": "FOO", "value": "bar", "is_secret": False},
            {"name": "TOKEN", "value": "abc", "is_secret": True},
        ],
    )
    override = await resolve_sandbox_env(str(env.id))
    assert isinstance(override, SandboxEnvOverride)
    assert override.base_image == "python:3.12"
    assert override.memory_bytes == 4_000_000_000
    assert override.env_vars == {"FOO": "bar", "TOKEN": "abc"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_global_default_returns_none_when_no_row_and_no_env_lock(monkeypatch):
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: False)
    assert await get_global_default() is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_global_default_returns_row_values(monkeypatch):
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL,
        name="Default",
        base_image="python:3.12",
        memory_bytes=2_000_000_000,
        cpus=Decimal("1.5"),
        is_default=True,
    )
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: False)
    override = await get_global_default()
    assert override is not None
    assert override.base_image == "python:3.12"
    assert override.memory_bytes == 2_000_000_000
    assert override.cpus == 1.5


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_global_default_applies_env_lock_overlay(monkeypatch):
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="row-image", memory_bytes=1_000_000_000, is_default=True
    )
    monkeypatch.setattr(
        "core.site_settings.site_settings.is_env_locked", lambda name: name in {"sandbox_base_image", "sandbox_memory"}
    )
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_base_image", "env-image")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_memory", 9_000_000_000)
    override = await get_global_default()
    assert override.base_image == "env-image"
    assert override.memory_bytes == 9_000_000_000
