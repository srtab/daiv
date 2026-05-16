from decimal import Decimal

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.services import SandboxEnvOverride, get_global_default, merge_sandbox_runtime, resolve_sandbox_env

from accounts.models import User
from codebase.repo_config import Sandbox, SandboxCommandPolicy


@pytest.mark.asyncio
async def test_resolve_returns_none_for_none_id():
    assert await resolve_sandbox_env(None) is None
    assert await resolve_sandbox_env("") is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_raises_lookup_error_for_missing():
    import uuid

    with pytest.raises(LookupError, match="not found"):
        await resolve_sandbox_env(str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_resolve_raises_lookup_error_for_malformed_uuid():
    with pytest.raises(LookupError, match="Malformed"):
        await resolve_sandbox_env("not-a-uuid")


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
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: name == "sandbox_memory")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_memory", 9_000_000_000)
    override = await get_global_default()
    assert override.memory_bytes == 9_000_000_000


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_global_default_base_image_is_not_lockable(monkeypatch):
    """``DAIV_SANDBOX_BASE_IMAGE`` no longer overlays the row at runtime; the UI value wins."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="row-image", is_default=True
    )
    # Even if the env var is "locked", base_image is not in the lockable set.
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: name == "sandbox_base_image")
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_base_image", "env-image")
    override = await get_global_default()
    assert override.base_image == "row-image"


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


def test_locked_field_ignores_per_run_and_yml():
    """A field listed in ``locked_fields`` must read only from the global default
    (which carries the DAIV_SANDBOX_* overlay); per-run and ``.daiv.yml`` are
    ignored even when they set a non-None value."""
    sb, fields = _yaml_sandbox(memory_bytes=8_000_000_000, cpus=4.0)
    out = merge_sandbox_runtime(
        sb,
        fields,
        per_run=_override(memory_bytes=4_000_000_000, cpus=2.0),
        global_default=_override(memory_bytes=2_000_000_000, cpus=1.0),
        locked_fields=frozenset({"memory_bytes"}),
    )
    # locked: global wins despite per-run + yml
    assert out.memory_bytes == 2_000_000_000
    # unlocked: per-run still wins
    assert out.cpus == 2.0


def test_locked_field_without_global_falls_through_to_runtime_default():
    sb, fields = _yaml_sandbox(network_enabled=True)
    out = merge_sandbox_runtime(
        sb,
        fields,
        per_run=_override(network_enabled=True),
        global_default=None,
        locked_fields=frozenset({"network_enabled"}),
    )
    # Locked + no global => runtime default (False) wins; per-run/yml are ignored.
    assert out.network_enabled is False


def test_per_run_base_image_wins_when_env_var_says_locked(monkeypatch):
    """Regression: env-locked ``sandbox_base_image`` must not block per-run / .daiv.yml.

    The whole point of removing ``base_image`` from the lock set is that
    per-environment images are admin-controlled. Even when the env var is set,
    ``get_locked_runtime_fields()`` returns an empty set for base_image, so
    ``merge_sandbox_runtime`` respects per-run and yml overrides.
    """
    from sandbox_envs.services import get_locked_runtime_fields

    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: name == "sandbox_base_image")
    assert "base_image" not in get_locked_runtime_fields()

    sb, fields = _yaml_sandbox(base_image="yml-image")
    out = merge_sandbox_runtime(
        sb,
        fields,
        per_run=_override(base_image="per-run-image"),
        global_default=_override(base_image="row-image"),
        locked_fields=get_locked_runtime_fields(),
    )
    assert out.base_image == "per-run-image"


def test_get_locked_runtime_fields_returns_locked_subset(monkeypatch):
    from sandbox_envs.services import get_locked_runtime_fields

    # ``sandbox_base_image`` is set but is no longer in the lockable set —
    # it must not appear in the returned frozenset.
    monkeypatch.setattr(
        "core.site_settings.site_settings.is_env_locked",
        lambda name: name in {"sandbox_base_image", "sandbox_memory", "sandbox_cpu"},
    )
    locked = get_locked_runtime_fields()
    assert locked == frozenset({"memory_bytes", "cpus"})


@pytest.mark.django_db(transaction=True)
def test_humanise_global_default_with_full_row(monkeypatch):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL,
        name="Default",
        base_image="python:3.14",
        network_enabled=True,
        memory_bytes=2 * 2**30,
        cpus=Decimal("1.5"),
        is_default=True,
    )
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: False)
    from sandbox_envs.services import humanise_global_default

    summary = humanise_global_default()
    assert summary == {
        "network": "enabled",
        "memory": "2 GiB",
        "cpus": "1.5",
        "has_network": True,
        "has_memory": True,
        "has_cpus": True,
    }


@pytest.mark.django_db(transaction=True)
def test_humanise_global_default_empty_returns_none_marks(monkeypatch):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: False)
    from sandbox_envs.services import humanise_global_default

    summary = humanise_global_default()
    assert summary == {
        "network": "",
        "memory": "",
        "cpus": "",
        "has_network": False,
        "has_memory": False,
        "has_cpus": False,
    }


@pytest.mark.django_db(transaction=True)
def test_humanise_global_default_memory_in_mib_when_not_whole_gib(monkeypatch):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.14", memory_bytes=768 * 2**20, is_default=True
    )
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: False)
    from sandbox_envs.services import humanise_global_default

    assert humanise_global_default()["memory"] == "768 MiB"


@pytest.mark.django_db(transaction=True)
def test_humanise_global_default_uses_env_lock_overlay(monkeypatch):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL,
        name="Default",
        base_image="python:3.14",
        network_enabled=False,
        memory_bytes=512 * 2**20,
        cpus=Decimal("0.5"),
        is_default=True,
    )
    monkeypatch.setattr(
        "core.site_settings.site_settings.is_env_locked",
        lambda name: name in {"sandbox_network_enabled", "sandbox_memory"},
    )
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_network_enabled", True)
    monkeypatch.setattr("core.site_settings.site_settings.sandbox_memory", 4 * 2**30)
    from sandbox_envs.services import humanise_global_default

    summary = humanise_global_default()
    # Locked fields use the env-var values, not the row values.
    assert summary["network"] == "enabled"
    assert summary["memory"] == "4 GiB"
    # Unlocked field still falls back to the row value.
    assert summary["cpus"] == "0.5"
