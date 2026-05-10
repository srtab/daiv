from decimal import Decimal

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.services import SandboxEnvOverride, get_global_default, merge_sandbox_runtime, resolve_sandbox_env

from accounts.models import User
from codebase.repo_config import Sandbox, SandboxCommandPolicy


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


def _yaml_sandbox(**explicit) -> tuple[Sandbox, frozenset[str]]:
    """Helper: build a Sandbox like `.daiv.yml` parsing would, returning the model
    and the set of keys that were explicitly provided."""
    sb = Sandbox.model_validate(explicit)
    return sb, frozenset(sb.model_fields_set)


def _override(**fields) -> SandboxEnvOverride:
    defaults: dict = {"base_image": None, "network_enabled": None, "memory_bytes": None, "cpus": None, "env_vars": {}}
    defaults.update(fields)
    return SandboxEnvOverride(**defaults)


def test_per_run_wins_over_everything():
    sb, fields = _yaml_sandbox(memory_bytes=8_000_000_000)
    out = merge_sandbox_runtime(
        sb, fields, per_run=_override(memory_bytes=4_000_000_000), global_default=_override(memory_bytes=2_000_000_000)
    )
    assert out.memory_bytes == 4_000_000_000


def test_daiv_yml_beats_global_when_key_present():
    sb, fields = _yaml_sandbox(memory_bytes=8_000_000_000)
    out = merge_sandbox_runtime(sb, fields, per_run=None, global_default=_override(memory_bytes=2_000_000_000))
    assert out.memory_bytes == 8_000_000_000


def test_daiv_yml_explicit_null_beats_global():
    sb, fields = _yaml_sandbox(memory_bytes=None)  # explicit null
    out = merge_sandbox_runtime(sb, fields, per_run=None, global_default=_override(memory_bytes=2_000_000_000))
    assert out.memory_bytes is None


def test_global_fills_when_yml_absent():
    sb, fields = _yaml_sandbox()  # no keys
    out = merge_sandbox_runtime(sb, fields, per_run=None, global_default=_override(memory_bytes=2_000_000_000))
    assert out.memory_bytes == 2_000_000_000


def test_default_when_no_source():
    sb, fields = _yaml_sandbox()
    out = merge_sandbox_runtime(sb, fields, per_run=None, global_default=None)
    assert out.memory_bytes is None
    assert out.base_image is None
    assert out.network_enabled is False  # bool default at runtime
    assert out.cpus is None
    assert out.env_vars == {}


def test_env_vars_per_key_merge():
    sb, fields = _yaml_sandbox()
    out = merge_sandbox_runtime(
        sb,
        fields,
        per_run=_override(env_vars={"B": "99", "C": "3"}),
        global_default=_override(env_vars={"A": "1", "B": "2"}),
    )
    assert out.env_vars == {"A": "1", "B": "99", "C": "3"}


def test_command_policy_always_from_yml():
    policy = SandboxCommandPolicy(allow=("git ",))
    sb = Sandbox(command_policy=policy)
    fields = frozenset(sb.model_fields_set)
    out = merge_sandbox_runtime(sb, fields, per_run=_override(), global_default=_override())
    assert out.command_policy is policy


def test_enabled_iff_base_image_set():
    sb, fields = _yaml_sandbox()
    out_disabled = merge_sandbox_runtime(sb, fields, per_run=None, global_default=_override())
    assert out_disabled.enabled is False

    out_enabled = merge_sandbox_runtime(sb, fields, per_run=None, global_default=_override(base_image="python:3.12"))
    assert out_enabled.enabled is True
