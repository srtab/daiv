from decimal import Decimal

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.services import SandboxEnvOverride, get_global_default, humanise_env_summary, resolve_sandbox_env

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
        network_enabled=True,
        memory_bytes=2 * 2**30,
        cpus=Decimal("1.5"),
        is_default=True,
    )
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
def test_humanise_global_default_empty_returns_none_marks():
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
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
def test_humanise_global_default_memory_in_mib_when_not_whole_gib():
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.14", memory_bytes=768 * 2**20, is_default=True
    )
    from sandbox_envs.services import humanise_global_default

    assert humanise_global_default()["memory"] == "768 MiB"


@pytest.mark.django_db
class TestHumaniseEnvSummary:
    def test_user_env_with_all_fields(self):
        user = User.objects.create(username="u1", email="u1@example.com")
        env = SandboxEnvironment.objects.create(
            scope=Scope.USER,
            user=user,
            name="rust",
            base_image="rust:1.83",
            cpus=Decimal("2"),
            memory_bytes=4 * 2**30,
            network_enabled=True,
        )
        assert humanise_env_summary(env) == "rust:1.83 · 2 CPU · 4 GiB · net"

    def test_user_env_minimal_falls_back_to_base_image(self):
        user = User.objects.create(username="u2", email="u2@example.com")
        env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="bare", base_image="alpine:3.20")
        assert humanise_env_summary(env) == "alpine:3.20"

    def test_user_env_with_no_base_image_returns_empty(self):
        user = User.objects.create(username="u3", email="u3@example.com")
        env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="x")
        assert humanise_env_summary(env) == ""

    def test_user_env_fractional_cpu_and_mib(self):
        user = User.objects.create(username="u4", email="u4@example.com")
        env = SandboxEnvironment.objects.create(
            scope=Scope.USER,
            user=user,
            name="tiny",
            base_image="busybox",
            cpus=Decimal("0.5"),
            memory_bytes=512 * 2**20,
        )
        assert humanise_env_summary(env) == "busybox · 0.5 CPU · 512 MiB"

    def test_user_env_network_disabled_no_suffix(self):
        user = User.objects.create(username="u5", email="u5@example.com")
        env = SandboxEnvironment.objects.create(
            scope=Scope.USER, user=user, name="off", base_image="alpine", network_enabled=False
        )
        assert humanise_env_summary(env) == "alpine"

    def test_default_summary_uses_row_fields(self):
        SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=True).update(
            base_image="python:3.14-slim", cpus=Decimal("1"), memory_bytes=2 * 2**30, network_enabled=True
        )
        env = SandboxEnvironment.objects.get(scope=Scope.GLOBAL, is_default=True)
        assert humanise_env_summary(env) == "python:3.14-slim · 1 CPU · 2 GiB · net"

    def test_global_non_default_uses_row_fields(self):
        env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL,
            name="staging",
            base_image="alpine",
            cpus=Decimal("2"),
            memory_bytes=2 * 2**30,
            network_enabled=True,
            is_default=False,
        )
        assert humanise_env_summary(env) == "alpine · 2 CPU · 2 GiB · net"


@pytest.mark.django_db
class TestResolveEnvForRun:
    @pytest.fixture(autouse=True)
    def _clear_global_envs(self):
        """Remove migration-seeded global envs so each test starts from scratch."""
        SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()

    def _user(self, name="u"):
        return User.objects.create(username=name, email=f"{name}@x.test")

    def test_returns_none_when_no_repo_and_no_global_default(self):
        user = self._user()
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        result = async_to_sync(resolve_env_for_run)(user=user, repo_id=None)
        assert result is None

    def test_returns_global_default_when_no_repo(self):
        user = self._user()
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        default_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id=None)
        assert result == default_env

    def test_returns_user_env_matching_repo(self):
        user = self._user()
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        user_env = SandboxEnvironment.objects.create(
            scope=Scope.USER, user=user, name="me", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == user_env

    def test_user_env_beats_global_env_for_same_repo(self):
        user = self._user()
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        user_env = SandboxEnvironment.objects.create(
            scope=Scope.USER, user=user, name="my-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == user_env

    def test_global_env_matches_when_no_user_env(self):
        user = self._user()
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        global_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == global_env

    def test_falls_back_to_global_default_when_no_match(self):
        user = self._user()
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        default_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
        )
        SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["other/repo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == default_env

    def test_does_not_return_other_users_user_env(self):
        u1 = self._user("u1")
        u2 = self._user("u2")
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        default_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
        )
        SandboxEnvironment.objects.create(
            scope=Scope.USER, user=u2, name="theirs", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=u1, repo_id="acme/foo")
        assert result == default_env

    def test_anonymous_user_uses_global_only(self):
        from asgiref.sync import async_to_sync
        from sandbox_envs.services import resolve_env_for_run

        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        global_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=None, repo_id="acme/foo")
        assert result == global_env


@pytest.mark.django_db
class TestResolveEnvForRunSync:
    @pytest.fixture(autouse=True)
    def _clear_global_envs(self):
        """Remove migration-seeded global envs so each test starts from scratch."""
        SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()

    def test_sync_wrapper_returns_same_env(self):
        from sandbox_envs.services import resolve_env_for_run_sync

        user = User.objects.create(username="u", email="u@x.test")
        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        env = SandboxEnvironment.objects.create(
            scope=Scope.USER, user=user, name="me", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = resolve_env_for_run_sync(user=user, repo_id="acme/foo")
        assert result == env


@pytest.mark.django_db
def test_get_global_default_returns_row_values_without_overlay():
    """After the env-lock removal, get_global_default just reads the row."""
    from asgiref.sync import async_to_sync
    from sandbox_envs.services import get_global_default

    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL,
        name="Default",
        base_image="python:3.14",
        cpus=2.0,
        memory_bytes=2 * 2**30,
        network_enabled=True,
        is_default=True,
    )

    override = async_to_sync(get_global_default)()
    assert override is not None
    assert override.base_image == "python:3.14"
    assert override.cpus == 2.0
    assert override.memory_bytes == 2 * 2**30
    assert override.network_enabled is True


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
