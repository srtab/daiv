from decimal import Decimal

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.services import alist_visible_environments, build_env_trigger, get_global_default, resolve_sandbox_env
from sandbox_envs.spec import SandboxEnvOverride

from accounts.models import User


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
async def test_get_global_default_returns_none_when_no_row():
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    assert await get_global_default() is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_global_default_returns_row_values():
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL,
        name="Default",
        base_image="python:3.12",
        memory_bytes=2_000_000_000,
        cpus=Decimal("1.5"),
        is_default=True,
    )
    override = await get_global_default()
    assert override is not None
    assert override.base_image == "python:3.12"
    assert override.memory_bytes == 2_000_000_000
    assert override.cpus == 1.5


@pytest.mark.django_db(transaction=True)
def test_humanise_global_default_with_full_row():
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL,
        name="Default",
        base_image="python:3.14",
        memory_bytes=2 * 2**30,
        cpus=Decimal("1.5"),
        is_default=True,
    )
    from sandbox_envs.services import humanise_global_default

    summary = humanise_global_default()
    assert summary == {"memory": "2 GiB", "cpus": "1.5", "has_memory": True, "has_cpus": True}


@pytest.mark.django_db(transaction=True)
def test_humanise_global_default_empty_returns_none_marks():
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    from sandbox_envs.services import humanise_global_default

    summary = humanise_global_default()
    assert summary == {"memory": "", "cpus": "", "has_memory": False, "has_cpus": False}


@pytest.mark.django_db(transaction=True)
def test_humanise_global_default_memory_in_mib_when_not_whole_gib():
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.14", memory_bytes=768 * 2**20, is_default=True
    )
    from sandbox_envs.services import humanise_global_default

    assert humanise_global_default()["memory"] == "768 MiB"


@pytest.mark.django_db
def test_get_global_default_returns_row_values_without_overlay():
    """After the env-lock removal, get_global_default just reads the row.
    Network state is now derived from egress_policy presence, not a separate column."""
    from asgiref.sync import async_to_sync
    from sandbox_envs.services import get_global_default

    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL,
        name="Default",
        base_image="python:3.14",
        cpus=2.0,
        memory_bytes=2 * 2**30,
        egress_policy={"default": "allow", "intercept": "all", "rules": []},
        is_default=True,
    )

    override = async_to_sync(get_global_default)()
    assert override is not None
    assert override.base_image == "python:3.14"
    assert override.cpus == 2.0
    assert override.memory_bytes == 2 * 2**30
    # egress_policy is set ⇒ egress is populated (network is on)
    assert override.egress is not None


def test_get_locked_runtime_fields_is_gone():
    """The lock infrastructure is removed — the symbol should no longer exist."""
    import sandbox_envs.services as svc

    assert not hasattr(svc, "FIELD_TO_LOCK_SETTING")
    assert not hasattr(svc, "get_locked_runtime_fields")


def test_site_settings_has_no_sandbox_resource_fields():
    """The deployment-level sandbox overrides are removed; only timeout and api_key remain."""
    from core.site_settings import site_settings

    for removed in (
        "sandbox_base_image",
        "sandbox_ephemeral",
        "sandbox_network_enabled",
        "sandbox_cpu",
        "sandbox_memory",
    ):
        assert removed not in site_settings.FIELD_DEFAULTS
    assert "sandbox_timeout" in site_settings.FIELD_DEFAULTS


@pytest.fixture
def global_env(db):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    return SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="g", base_image="alpine")


def test_build_env_trigger_created_shape(global_env):
    payload = build_env_trigger(global_env, "created")
    assert payload == {
        "env-created": {
            "id": str(global_env.id),
            "name": "g",
            "scope": "global",
            "scope_display": global_env.get_scope_display(),
            "is_default": False,
            "summary": "alpine",
        }
    }


def test_build_env_trigger_updated_uses_action_key(global_env):
    assert "env-updated" in build_env_trigger(global_env, "updated")


def test_build_env_trigger_deleted_uses_action_key(global_env):
    assert "env-deleted" in build_env_trigger(global_env, "deleted")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_row_to_override_builds_egress():
    from sandbox_envs.services import row_to_override

    env = await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL,
        name="eg",
        base_image="python:3.12",
        egress_policy={
            "default": "deny",
            "intercept": "credentialed",
            "rules": [{"host": "*.github.com", "methods": ["GET"], "inject": "gh"}],
        },
        egress_secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
    )
    override = row_to_override(env)
    assert override.egress is not None
    assert override.egress.policy.rules[0].host == "*.github.com"
    assert override.egress.secrets["gh"].value.get_secret_value() == "Bearer t"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_row_to_override_fails_closed_to_none_when_malformed():
    from sandbox_envs.services import row_to_override

    # Dangling inject (no matching secret) — stored directly, bypassing clean(). The env intended
    # restricted egress but its config is unusable; fail closed to NO network (egress=None →
    # network_mode=none). The old deny-all-with-plumbing state no longer exists and would be rejected
    # by the sandbox (422 "permits nothing").
    env = await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL,
        name="bad",
        base_image="python:3.12",
        egress_policy={"default": "deny", "rules": [{"host": "x", "inject": "missing"}]},
        egress_secrets={},
    )
    override = row_to_override(env)
    assert override.egress is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_row_to_override_fails_closed_to_none_on_decryption_error(mocker):
    from sandbox_envs.services import row_to_override

    from core.encryption import DecryptionError

    env = await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL,
        name="rotated",
        base_image="python:3.12",
        egress_policy={"default": "deny", "rules": [{"host": "*.github.com", "inject": "gh"}]},
        egress_secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
    )

    # Simulate a rotated/lost DAIV_ENCRYPTION_KEY: the still-present egress_policy can no longer be
    # paired with its decryptable secrets. The env intended restricted egress but its config is unusable;
    # fail closed to NO network (egress=None → network_mode=none). The old deny-all-with-plumbing state
    # no longer exists and would be rejected by the sandbox.
    def _raise(instance):
        raise DecryptionError("bad key")

    mocker.patch.object(type(env), "egress_secrets", new_callable=lambda: property(fget=_raise))
    override = row_to_override(env)
    assert override.egress is None


def test_row_to_override_fails_closed_to_none_no_db():
    """Non-DB guard: dangling inject triggers the except branch → egress=None, no network."""
    from types import SimpleNamespace

    from sandbox_envs.services import row_to_override

    # Build an unsaved mock that provides exactly the attributes row_to_override reads (including the
    # derived `is_networked`, which a real model computes from egress_policy). egress_policy has a
    # dangling inject ("missing" is not in egress_secrets={}) so EgressConfigRequest.from_stored
    # raises, hitting the fail-closed except branch.
    env = SimpleNamespace(
        id="test-no-db",
        name="no-db",
        env_vars=[],
        is_networked=True,
        egress_policy={"default": "deny", "rules": [{"host": "example.com", "inject": "missing"}]},
        egress_secrets={},
        base_image="python:3.12",
        memory_bytes=None,
        cpus=None,
    )
    override = row_to_override(env)
    # The except branch (dangling inject → from_stored raises) must have been taken.
    assert override.egress is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_row_to_override_drops_env_vars_on_decryption_error(mocker):
    """A rotated/lost DAIV_ENCRYPTION_KEY must not crash an agent run: env_vars that can no longer be
    decrypted are dropped (override.env_vars == {}) and the run proceeds. This is the agent-run analogue
    of the form-layer decryption guard, distinct from the egress fail-closed branch."""
    from sandbox_envs.services import row_to_override

    from core.encryption import DecryptionError

    env = await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL,
        name="rotated-vars",
        base_image="python:3.12",
        env_vars=[{"name": "API_KEY", "value": "s3cr3t", "is_secret": True}],
    )

    def _raise(instance):
        raise DecryptionError("bad key")

    mocker.patch.object(type(env), "env_vars", new_callable=lambda: property(fget=_raise))
    override = row_to_override(env)
    assert override.env_vars == {}


@pytest.mark.django_db(transaction=True)
async def test_alist_visible_environments_paginates_by_scope_name_id():
    """``after`` resumes after the cursor row in (scope, name, id) order; every visible
    env is returned once with no gaps or repeats."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="ve", email="ve@e.com", password="x")  # noqa: S106
    for name in ("alpha", "bravo", "charlie", "delta", "echo"):
        await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name=name, base_image="x")

    collected: list[str] = []
    after = None
    for _ in range(10):
        page = await alist_visible_environments(user, limit=2, after=after)
        if not page:
            break
        collected.extend(e.name for e in page)
        last = page[-1]
        after = (last.scope, last.name, last.id)

    assert collected == ["alpha", "bravo", "charlie", "delta", "echo"]


@pytest.mark.django_db(transaction=True)
async def test_alist_visible_environments_excludes_other_users():
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="ve2", email="ve2@e.com", password="x")  # noqa: S106
    other = await User.objects.acreate_user(username="ve3", email="ve3@e.com", password="x")  # noqa: S106
    await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name="mine", base_image="x")
    await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=other, name="theirs", base_image="x")

    rows = await alist_visible_environments(user)
    names = {e.name for e in rows}
    assert "mine" in names
    assert "theirs" not in names
