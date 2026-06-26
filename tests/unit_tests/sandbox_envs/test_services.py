from decimal import Decimal

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.services import SandboxEnvOverride, build_env_trigger, get_global_default, resolve_sandbox_env

from accounts.models import User


def _sandbox_runtime(*, base_image="python:3.12", egress=None):
    from codebase.context import SandboxRuntime
    from core.sandbox.command_policy import SandboxCommandPolicy

    return SandboxRuntime(
        base_image=base_image,
        memory_bytes=None,
        cpus=None,
        env_vars={},
        command_policy=SandboxCommandPolicy(),
        egress=egress,
    )


def test_augment_skips_when_network_off():
    from unittest.mock import Mock

    from sandbox_envs.services import augment_sandbox_with_platform_egress

    # Network off == no egress policy: augment must not resolve a credential or build a policy.
    sb = _sandbox_runtime(egress=None)
    client = Mock()
    out = augment_sandbox_with_platform_egress(sb, client, Mock())
    assert out is sb
    client.get_git_egress_credential.assert_not_called()


def test_augment_skips_when_sandbox_disabled():
    from unittest.mock import Mock

    from sandbox_envs.services import augment_sandbox_with_platform_egress

    from core.sandbox.schemas import EgressConfigRequest

    # Networked (egress set) but sandbox disabled (base_image=None → enabled == False): still a skip.
    sb = _sandbox_runtime(base_image=None, egress=EgressConfigRequest())
    client = Mock()
    out = augment_sandbox_with_platform_egress(sb, client, Mock())
    assert out is sb
    client.get_git_egress_credential.assert_not_called()


def test_augment_adds_platform_egress_when_network_on():
    from unittest.mock import Mock

    from pydantic import SecretStr
    from sandbox_envs.services import PLATFORM_EGRESS_SECRET_NAME, augment_sandbox_with_platform_egress

    from codebase.clients.base import GitEgressCredential
    from core.sandbox.schemas import EgressConfigRequest

    sb = _sandbox_runtime(egress=EgressConfigRequest())
    client = Mock()
    client.get_git_egress_credential.return_value = GitEgressCredential(
        host="gitlab.example.com", value=SecretStr("Basic abc")
    )
    repo = Mock()

    out = augment_sandbox_with_platform_egress(sb, client, repo)

    client.get_git_egress_credential.assert_called_once_with(repo)
    assert out.egress.policy.rules[0].host == "gitlab.example.com"
    assert PLATFORM_EGRESS_SECRET_NAME in out.egress.secrets


def test_apply_platform_egress_returns_unchanged_when_no_credential():
    from sandbox_envs.services import apply_platform_egress

    from core.sandbox.schemas import EgressConfigRequest

    egress = EgressConfigRequest()
    assert apply_platform_egress(egress, None) is egress


def test_apply_platform_egress_builds_deny_all_base_and_injects():
    from pydantic import SecretStr
    from sandbox_envs.services import PLATFORM_EGRESS_SECRET_NAME, apply_platform_egress

    from codebase.clients.base import GitEgressCredential

    cred = GitEgressCredential(host="gitlab.example.com", value=SecretStr("Basic abc"))
    result = apply_platform_egress(None, cred)

    assert result.policy.default == "deny"
    assert result.policy.intercept == "all"
    rule = result.policy.rules[0]
    assert rule.host == "gitlab.example.com"
    assert rule.methods == ["*"]
    assert rule.inject == PLATFORM_EGRESS_SECRET_NAME
    secret = result.secrets[PLATFORM_EGRESS_SECRET_NAME]
    assert secret.header == "Authorization"
    assert secret.value.get_secret_value() == "Basic abc"


def test_apply_platform_egress_preserves_env_policy_and_prepends():
    from pydantic import SecretStr
    from sandbox_envs.services import apply_platform_egress

    from codebase.clients.base import GitEgressCredential
    from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

    env = EgressConfigRequest(
        policy=EgressPolicy(
            default="deny",
            intercept="credentialed",
            rules=[EgressRule(host="api.openai.com", methods=["GET"], inject="s1")],
        ),
        secrets={"s1": EgressSecret(header="Authorization", value=SecretStr("sk"))},
    )
    cred = GitEgressCredential(host="github.com", value=SecretStr("Basic xyz"))
    result = apply_platform_egress(env, cred)

    assert result.policy.intercept == "credentialed"  # env knob preserved
    assert [r.host for r in result.policy.rules] == ["github.com", "api.openai.com"]  # prepended
    assert set(result.secrets) == {"s1", "__daiv_git_platform__"}


def test_apply_platform_egress_reachability_only_when_no_token():
    from sandbox_envs.services import PLATFORM_EGRESS_SECRET_NAME, apply_platform_egress

    from codebase.clients.base import GitEgressCredential

    cred = GitEgressCredential(host="gitlab.example.com", value=None)
    result = apply_platform_egress(None, cred)

    assert result.policy.rules[0].host == "gitlab.example.com"
    assert result.policy.rules[0].inject is None
    assert PLATFORM_EGRESS_SECRET_NAME not in result.secrets


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
class TestResolveEnvForRun:
    @pytest.fixture(autouse=True)
    def _clearglobal_envs(self):
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

    def test_user_env_beatsglobal_env_for_same_repo(self):
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

    def testglobal_env_matches_when_no_user_env(self):
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


@pytest.mark.django_db
class TestMergeSandboxRuntime:
    def test_per_run_env_supplies_fields_when_set(self):
        from pydantic import SecretStr
        from sandbox_envs.services import SandboxEnvOverride, merge_sandbox_runtime

        from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

        egress_on = EgressConfigRequest(
            policy=EgressPolicy(rules=[EgressRule(host="per-run.example", inject="t")]),
            secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
        )
        per_run = SandboxEnvOverride(
            base_image="python:3.14", memory_bytes=2 * 2**30, cpus=2.0, env_vars={"K": "v"}, egress=egress_on
        )
        global_default = SandboxEnvOverride(
            base_image="python:3.12", memory_bytes=1 * 2**30, cpus=1.0, env_vars={"G": "g"}, egress=None
        )
        runtime = merge_sandbox_runtime(per_run=per_run, global_default=global_default)
        assert runtime.base_image == "python:3.14"
        # per-run egress takes precedence; network is on (egress is not None)
        assert runtime.egress is not None
        assert runtime.egress.policy.rules[0].host == "per-run.example"
        assert runtime.memory_bytes == 2 * 2**30
        assert runtime.cpus == 2.0
        assert runtime.env_vars == {"G": "g", "K": "v"}

    def test_per_run_off_blocks_global_egress_inheritance(self):
        from pydantic import SecretStr
        from sandbox_envs.services import SandboxEnvOverride, merge_sandbox_runtime

        from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

        egress_global = EgressConfigRequest(
            policy=EgressPolicy(rules=[EgressRule(host="global.example", inject="t")]),
            secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
        )
        per_run = SandboxEnvOverride(base_image=None, memory_bytes=None, cpus=None, env_vars={"K": "v"}, egress=None)
        global_default = SandboxEnvOverride(
            base_image="python:3.12", memory_bytes=1 * 2**30, cpus=1.0, env_vars={"G": "g"}, egress=egress_global
        )
        runtime = merge_sandbox_runtime(per_run=per_run, global_default=global_default)
        assert runtime.base_image == "python:3.12"
        # per-run has no egress (off); global_default's egress is NOT inherited
        # (network is per-env, not fallthrough — per-run off beats global on)
        assert runtime.egress is None
        assert runtime.memory_bytes == 1 * 2**30
        assert runtime.cpus == 1.0
        assert runtime.env_vars == {"G": "g", "K": "v"}

    def test_per_run_env_vars_shadow_global_on_key_collision(self):
        from sandbox_envs.services import SandboxEnvOverride, merge_sandbox_runtime

        per_run = SandboxEnvOverride(
            base_image=None, memory_bytes=None, cpus=None, env_vars={"SHARED": "from-per-run", "PER_RUN_ONLY": "x"}
        )
        global_default = SandboxEnvOverride(
            base_image="python:3.12",
            memory_bytes=None,
            cpus=None,
            env_vars={"SHARED": "from-global", "GLOBAL_ONLY": "g"},
        )
        runtime = merge_sandbox_runtime(per_run=per_run, global_default=global_default)
        assert runtime.env_vars == {"SHARED": "from-per-run", "PER_RUN_ONLY": "x", "GLOBAL_ONLY": "g"}

    def test_command_policy_defaults_empty(self):
        from sandbox_envs.services import SandboxEnvOverride, merge_sandbox_runtime

        from core.sandbox.command_policy import SandboxCommandPolicy

        per_run = SandboxEnvOverride(base_image="python:3.14", memory_bytes=None, cpus=None, env_vars={})
        runtime = merge_sandbox_runtime(per_run=per_run, global_default=None)
        assert runtime.command_policy == SandboxCommandPolicy()


@pytest.mark.django_db(transaction=True)
class TestAresolveRepoEnvs:
    """Direct coverage for aresolve_repo_envs precedence + edge cases.

    View-level tests exercise this indirectly through single-repo cases. These
    pin the in-memory precedence ladder and protect contracts shared by all
    batch call sites.
    """

    @pytest.fixture(autouse=True)
    def _clear_global(self):
        SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()

    @pytest.mark.asyncio
    async def test_explicit_env_id_stamps_all_targets(self):
        from activity.services import RepoTarget
        from sandbox_envs.services import aresolve_repo_envs

        resolved = await aresolve_repo_envs(
            user=None,
            repos=[RepoTarget(repo_id="a/b"), RepoTarget(repo_id="c/d")],
            explicit_env_id="00000000-0000-0000-0000-000000000099",
        )
        assert [t.sandbox_environment_id for t in resolved] == [
            "00000000-0000-0000-0000-000000000099",
            "00000000-0000-0000-0000-000000000099",
        ]

    @pytest.mark.asyncio
    async def test_empty_repos_returns_empty_list(self):
        from sandbox_envs.services import aresolve_repo_envs

        assert await aresolve_repo_envs(user=None, repos=[], explicit_env_id=None) == []
        assert await aresolve_repo_envs(user=None, repos=[], explicit_env_id="x") == []

    @pytest.mark.asyncio
    async def test_user_env_wins_over_global_when_both_match_repo(self):
        from activity.services import RepoTarget
        from sandbox_envs.services import aresolve_repo_envs

        user = await User.objects.acreate(username="u", email="u@x.test")
        user_env = await SandboxEnvironment.objects.acreate(
            scope=Scope.USER, user=user, name="mine", base_image="x", repo_ids=["a/b"]
        )
        await SandboxEnvironment.objects.acreate(scope=Scope.GLOBAL, name="theirs", base_image="x", repo_ids=["a/b"])
        resolved = await aresolve_repo_envs(user=user, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(user_env.id)

    @pytest.mark.asyncio
    async def test_global_repo_match_wins_over_default(self):
        from activity.services import RepoTarget
        from sandbox_envs.services import aresolve_repo_envs

        await SandboxEnvironment.objects.acreate(scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True)
        repo_env = await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="match", base_image="x", repo_ids=["a/b"]
        )
        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(repo_env.id)

    @pytest.mark.asyncio
    async def test_no_envs_at_all_yields_none(self):
        from activity.services import RepoTarget
        from sandbox_envs.services import aresolve_repo_envs

        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id is None

    @pytest.mark.asyncio
    async def test_envs_with_empty_repo_ids_do_not_match(self):
        """An env with an empty ``repo_ids`` list must not match any repo and must fall
        through to the GLOBAL default."""
        from activity.services import RepoTarget
        from sandbox_envs.services import aresolve_repo_envs

        default = await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True, repo_ids=[]
        )
        await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="empty", base_image="x", is_default=False, repo_ids=[]
        )
        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(default.id)

    @pytest.mark.asyncio
    async def test_user_scope_skipped_for_anonymous_or_none(self):
        from activity.services import RepoTarget
        from sandbox_envs.services import aresolve_repo_envs

        other = await User.objects.acreate(username="o", email="o@x.test")
        await SandboxEnvironment.objects.acreate(
            scope=Scope.USER, user=other, name="other-env", base_image="x", repo_ids=["a/b"]
        )
        default = await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True
        )
        # user=None must not leak the other user's USER env.
        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(default.id)

    @pytest.mark.asyncio
    async def test_input_targets_not_mutated(self):
        from activity.services import RepoTarget
        from sandbox_envs.services import aresolve_repo_envs

        await SandboxEnvironment.objects.acreate(scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True)
        original = [RepoTarget(repo_id="a/b"), RepoTarget(repo_id="c/d", ref="dev")]
        await aresolve_repo_envs(user=None, repos=original, explicit_env_id=None)
        assert all(t.sandbox_environment_id is None for t in original)
        assert original[1].ref == "dev"


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


def _egress_request(host: str):
    from pydantic import SecretStr

    from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

    return EgressConfigRequest(
        policy=EgressPolicy(rules=[EgressRule(host=host, inject="t")]),
        secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
    )


# _override was the old scaffolding helper (used network_enabled); replaced by _ov defined below.


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


def test_merge_prefers_per_run_egress():
    from sandbox_envs.services import merge_sandbox_runtime

    rt = merge_sandbox_runtime(
        per_run=_ov(egress=_egress_request("per-run.example")),
        global_default=_ov(egress=_egress_request("global.example")),
    )
    assert rt.egress.policy.rules[0].host == "per-run.example"


def test_merge_egress_is_none_when_neither_side_has_it():
    from sandbox_envs.services import merge_sandbox_runtime

    # Egress is opt-in: no policy on either side must never materialize one (it would otherwise
    # silently apply an unintended network posture).
    rt = merge_sandbox_runtime(per_run=_ov(egress=None), global_default=_ov(egress=None))
    assert rt.egress is None


# ---------------------------------------------------------------------------
# B3 tests — network_enabled removed from SandboxEnvOverride / SandboxRuntime
# ---------------------------------------------------------------------------

from sandbox_envs.services import merge_sandbox_runtime  # noqa: E402


@pytest.fixture
def make_egress():
    """Return a factory that builds an EgressConfigRequest for a single host."""

    def _factory(hosts: list[str]):
        from pydantic import SecretStr

        from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

        rules = [EgressRule(host=h, inject="t") for h in hosts]
        return EgressConfigRequest(
            policy=EgressPolicy(rules=rules),
            secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
        )

    return _factory


def _ov(**kw):
    base = {"base_image": None, "memory_bytes": None, "cpus": None, "env_vars": {}, "egress": None}
    base.update(kw)
    return SandboxEnvOverride(**base)


def test_sandbox_env_override_has_no_network_enabled():
    assert not hasattr(_ov(), "network_enabled")


def test_merge_takes_per_run_egress_off_even_when_global_has_policy(make_egress):
    # per-run env explicitly Off (egress=None) must NOT inherit the global default's policy.
    per_run = _ov(egress=None)
    global_default = _ov(egress=make_egress(["github.com"]))
    rt = merge_sandbox_runtime(per_run=per_run, global_default=global_default)
    assert rt.egress is None
    assert not hasattr(rt, "network_enabled")


def test_merge_falls_back_to_global_egress_when_no_per_run():
    eg = object()
    rt = merge_sandbox_runtime(per_run=None, global_default=_ov(egress=eg))
    assert rt.egress is eg
