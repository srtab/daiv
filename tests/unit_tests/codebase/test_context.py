from contextlib import ExitStack, contextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from automation.agent.middlewares.file_system import SandboxFileBackend
from automation.agent.middlewares.sandbox import SandboxMiddleware
from codebase.base import Scope as RepoScope
from codebase.clients.base import GitEgressCredential
from codebase.context import set_runtime_ctx
from codebase.exceptions import CloneRefNotFoundError
from core.sandbox.client import _run_sandbox_client, get_run_sandbox_client
from core.sandbox.schemas import EgressSecret
from tests.unit_tests.conftest import FakeSandboxClient, sandbox_spec


@pytest.fixture
def user_factory(db):
    from accounts.models import User

    counter = {"n": 0}

    async def _make():
        counter["n"] += 1
        return await User.objects.acreate_user(
            username=f"u{counter['n']}",
            email=f"u{counter['n']}@e.com",
            password="x",  # noqa: S106
        )

    return _make


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_builds_sandbox_field():
    """ctx.sandbox is populated from the GLOBAL default env when no per-run env is supplied."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.12", memory_bytes=2_000_000_000, is_default=True
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None  # these tests assert env resolution, not egress
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.GLOBAL) as ctx:
                assert ctx.sandbox.base_image == "python:3.12"
                assert ctx.sandbox.memory_bytes == 2_000_000_000


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_honors_per_run_env(user_factory):
    user = await user_factory()
    env = await SandboxEnvironment.objects.acreate(
        scope=Scope.USER, user=user, name="dev", base_image="alpine:latest", memory_bytes=1_000_000_000
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None  # these tests assert env resolution, not egress
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.GLOBAL, sandbox_env_id=str(env.id)) as ctx:
                assert ctx.sandbox.base_image == "alpine:latest"
                assert ctx.sandbox.memory_bytes == 1_000_000_000


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_auto_resolves_repo_env():
    """When no explicit sandbox_env_id is supplied, the resolver picks up a
    GLOBAL env whose repo_ids matches the run's repo_id."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.12", is_default=True
    )
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="django-env", base_image="python:3.14", repo_ids=["acme/foo"]
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None  # these tests assert env resolution, not egress
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="acme/foo", scope=RepoScope.GLOBAL) as ctx:
                assert ctx.sandbox.base_image == "python:3.14"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_auto_falls_back_to_global_default():
    """No repo_ids match → Auto returns the GLOBAL default."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.12", is_default=True
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None  # these tests assert env resolution, not egress
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="acme/foo", scope=RepoScope.GLOBAL) as ctx:
                assert ctx.sandbox.base_image == "python:3.12"


def _patch_context_deps(*, sandbox_enabled: bool):
    repo_client = MagicMock()
    repo_client.get_repository.return_value = MagicMock()
    repo_client.current_user.username = "daiv"
    repo_client.load_repo.return_value = nullcontext(MagicMock(working_dir="/tmp/repo"))  # noqa: S108
    sandbox = MagicMock()
    sandbox.enabled = sandbox_enabled
    # No egress policy + no derivable credential => augment_sandbox_with_platform_egress is a no-op
    # (it would otherwise call dataclasses.replace() on this MagicMock). These tests only exercise
    # transport open/close.
    sandbox.egress = None
    repo_client.get_git_egress_credential.return_value = None
    return _context_deps(repo_client, sandbox)


def _context_deps(repo_client, sandbox):
    """Patches that make ``set_runtime_ctx`` load ``repo_client``'s repo and resolve ``sandbox`` as the run's."""
    return (
        patch.multiple(
            "codebase.context",
            RepoClient=MagicMock(create_instance=MagicMock(return_value=repo_client)),
            RepositoryConfig=MagicMock(get_config=MagicMock(return_value=MagicMock(default_branch="main"))),
        ),
        patch("sandbox_envs.services.resolve_env_for_run", AsyncMock(return_value=None)),
        patch("sandbox_envs.services.get_global_default", AsyncMock(return_value=None)),
        patch("sandbox_envs.spec.merge_sandbox_spec", MagicMock(return_value=sandbox)),
        patch("sandbox_envs.services.row_to_override", MagicMock(return_value=None)),
    )


async def test_set_runtime_ctx_opens_and_closes_transport_when_sandbox_enabled():
    fake_client = MagicMock()
    fake_client.open = AsyncMock(return_value=fake_client)
    fake_client.close = AsyncMock()
    p = _patch_context_deps(sandbox_enabled=True)
    with p[0], p[1], p[2], p[3], p[4], patch("codebase.context.DAIVSandboxClient", return_value=fake_client):
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL):
            assert _run_sandbox_client.get() is fake_client
        fake_client.open.assert_awaited_once()
        fake_client.close.assert_awaited_once()
        assert _run_sandbox_client.get() is None


async def test_set_runtime_ctx_skips_transport_when_sandbox_disabled():
    p = _patch_context_deps(sandbox_enabled=False)
    with p[0], p[1], p[2], p[3], p[4], patch("codebase.context.DAIVSandboxClient") as ctor:
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL):
            assert _run_sandbox_client.get() is None
        ctor.assert_not_called()


async def test_set_runtime_ctx_injects_platform_egress_when_network_on():
    """Network-on integration seam: set_runtime_ctx must resolve the repo's git-platform credential
    via the client and land its allow-rule first on ``ctx.sandbox.egress`` (the only place all the
    unit-tested egress pieces are wired together). Guards against the augmentation line being dropped
    or reordered before merge_sandbox_spec — a regression every isolated unit test would miss."""
    from sandbox_envs.spec import SandboxSpec

    from codebase.clients.base import GitEgressCredential
    from core.sandbox.command_policy import SandboxCommandPolicy
    from core.sandbox.egress import PLATFORM_EGRESS_SECRET_NAME
    from core.sandbox.schemas import EgressConfigRequest

    repo_client = MagicMock()
    repository = MagicMock()
    repo_client.get_repository.return_value = repository
    repo_client.current_user.username = "daiv"
    repo_client.load_repo.return_value = nullcontext(MagicMock(working_dir="/tmp/repo"))  # noqa: S108
    repo_client.get_git_egress_credential.return_value = GitEgressCredential.for_token(
        host="github.com",
        token="tok",  # noqa: S106
    )

    # A real (frozen) SandboxSpec with a networked egress policy (egress is not None == network on);
    # augment_sandbox_with_platform_egress prepends the git-platform rule via dataclasses.replace().
    sandbox = SandboxSpec(
        base_image="python:3.12",
        memory_bytes=None,
        cpus=None,
        env_vars={},
        command_policy=SandboxCommandPolicy(),
        egress=EgressConfigRequest(),
    )
    fake_client = MagicMock()
    fake_client.open = AsyncMock(return_value=fake_client)
    fake_client.close = AsyncMock()

    with (
        patch.multiple(
            "codebase.context",
            RepoClient=MagicMock(create_instance=MagicMock(return_value=repo_client)),
            RepositoryConfig=MagicMock(get_config=MagicMock(return_value=MagicMock(default_branch="main"))),
            DAIVSandboxClient=MagicMock(return_value=fake_client),
        ),
        patch("sandbox_envs.services.resolve_env_for_run", AsyncMock(return_value=None)),
        patch("sandbox_envs.services.get_global_default", AsyncMock(return_value=None)),
        patch("sandbox_envs.spec.merge_sandbox_spec", MagicMock(return_value=sandbox)),
        patch("sandbox_envs.services.row_to_override", MagicMock(return_value=None)),
    ):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL) as ctx:
            repo_client.get_git_egress_credential.assert_called_once_with(repository)
            assert ctx.sandbox.egress is not None
            platform_rule = ctx.sandbox.egress.policy.rules[0]
            assert platform_rule.host == "github.com"
            assert platform_rule.inject == PLATFORM_EGRESS_SECRET_NAME
            assert PLATFORM_EGRESS_SECRET_NAME in ctx.sandbox.egress.secrets


async def test_set_runtime_ctx_resolves_platform_egress_after_clone():
    """Regression guard for the egress/clone token-divergence bug: the git-platform credential must be
    resolved AFTER load_repo() clones, so it observes any token the GitLab clone self-heal re-minted.
    The egress proxy overrides Authorization on every platform request, so a credential captured before
    a self-healing clone would pin the sidecar to the stale token the clone just discarded — breaking
    the in-sandbox push. Asserts clone happens before credential resolution."""
    from sandbox_envs.spec import SandboxSpec

    from codebase.clients.base import GitEgressCredential
    from core.sandbox.command_policy import SandboxCommandPolicy

    calls: list[str] = []
    repo_client = MagicMock()
    repository = MagicMock()
    repo_client.get_repository.return_value = repository
    repo_client.current_user.username = "daiv"

    clone_repo = MagicMock(working_dir="/tmp/repo")  # noqa: S108
    cm = MagicMock()
    cm.__enter__ = MagicMock(side_effect=lambda: calls.append("clone") or clone_repo)
    cm.__exit__ = MagicMock(return_value=False)
    repo_client.load_repo.return_value = cm

    def _cred(repo):
        calls.append("credential")
        return GitEgressCredential.for_token(host="github.com", token="tok")  # noqa: S106

    repo_client.get_git_egress_credential.side_effect = _cred

    # Network-off env (egress=None): a push token still opens it for the git platform host.
    sandbox = SandboxSpec(
        base_image="python:3.12",
        memory_bytes=None,
        cpus=None,
        env_vars={},
        command_policy=SandboxCommandPolicy(),
        egress=None,
    )
    fake_client = MagicMock()
    fake_client.open = AsyncMock(return_value=fake_client)
    fake_client.close = AsyncMock()

    with (
        patch.multiple(
            "codebase.context",
            RepoClient=MagicMock(create_instance=MagicMock(return_value=repo_client)),
            RepositoryConfig=MagicMock(get_config=MagicMock(return_value=MagicMock(default_branch="main"))),
            DAIVSandboxClient=MagicMock(return_value=fake_client),
        ),
        patch("sandbox_envs.services.resolve_env_for_run", AsyncMock(return_value=None)),
        patch("sandbox_envs.services.get_global_default", AsyncMock(return_value=None)),
        patch("sandbox_envs.spec.merge_sandbox_spec", MagicMock(return_value=sandbox)),
        patch("sandbox_envs.services.row_to_override", MagicMock(return_value=None)),
    ):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL) as ctx:
            assert calls == ["clone", "credential"], f"credential must resolve after clone, got {calls}"
            assert ctx.sandbox.egress is not None
            assert ctx.sandbox.egress.policy.rules[0].host == "github.com"


@contextmanager
def _network_off_run(credential: GitEgressCredential, working_dir: str):
    """Patch ``set_runtime_ctx``'s collaborators for a network-off env over a fake sandbox transport."""
    repo_client = MagicMock()
    repo_client.current_user.username = "daiv"
    repo_client.load_repo.return_value = nullcontext(MagicMock(working_dir=working_dir))
    repo_client.get_git_egress_credential.return_value = credential

    with ExitStack() as stack:
        for deps_patch in _context_deps(repo_client, sandbox_spec()):
            stack.enter_context(deps_patch)
        stack.enter_context(patch("codebase.context.DAIVSandboxClient", FakeSandboxClient))
        yield


@pytest.mark.parametrize("token", [pytest.param("tok", id="push-token"), pytest.param(None, id="token-less")])
async def test_set_runtime_ctx_opens_a_network_off_env_only_for_a_push_token(token):
    """B9: a network-off env reaches the git host only when a real push token exists."""
    credential = GitEgressCredential.for_token(host="github.com", token=token)

    with _network_off_run(credential, "/tmp/repo"):  # noqa: S108
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL) as ctx:
            egress = ctx.sandbox.egress

    if token:
        assert egress.policy.default == "deny"
        assert [rule.host for rule in egress.policy.rules] == ["github.com"]
        assert egress.policy.rules[0].inject in egress.secrets
    else:
        assert egress is None


@pytest.mark.parametrize("token", [pytest.param("tok", id="push-token"), pytest.param(None, id="token-less")])
async def test_a_network_off_sandbox_session_reaches_the_git_host_only_for_a_push_token(token, tmp_path):
    """B9: the session a network-off run starts carries egress only for the git host, only with a token."""
    (tmp_path / "README.md").write_text("hello\n")
    credential = GitEgressCredential.for_token(host="github.com", token=token)

    with _network_off_run(credential, str(tmp_path)):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL) as ctx:
            client = get_run_sandbox_client()
            middleware = SandboxMiddleware(
                agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
            )
            await middleware.abefore_agent({}, MagicMock(context=ctx))

    assert not client.is_open
    [session] = client.sessions.values()
    egress = session.request.egress
    if token:
        [rule] = egress.policy.rules
        assert (egress.policy.default, rule.host) == ("deny", "github.com")
        assert egress.secrets == {rule.inject: EgressSecret(header=credential.header, value=credential.value)}
    else:
        assert egress is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_falls_back_to_default_when_ref_missing():
    """With fallback enabled, a gone ref retries the clone on the default branch and records it."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()

    fake_repo = type("Repo", (), {})()
    good_cm = MagicMock()
    good_cm.__enter__ = MagicMock(return_value=fake_repo)
    good_cm.__exit__ = MagicMock(return_value=False)
    gone_cm = MagicMock()
    gone_cm.__enter__ = MagicMock(side_effect=CloneRefNotFoundError("gone", "r/p"))
    gone_cm.__exit__ = MagicMock(return_value=False)

    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x", "slug": "r/p"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None
        client.load_repo.side_effect = [gone_cm, good_cm]

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({"default_branch": "main"})
            async with set_runtime_ctx(
                repo_id="r/p", scope=RepoScope.GLOBAL, ref="gone", fallback_ref_on_missing=True
            ) as ctx:
                assert ctx.repo.ref == "main"

    assert client.load_repo.call_count == 2
    assert client.load_repo.call_args_list[0].kwargs["sha"] == "gone"
    assert client.load_repo.call_args_list[1].kwargs["sha"] == "main"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_reraises_missing_ref_without_fallback():
    """Default behavior (fallback disabled) propagates CloneRefNotFoundError unchanged."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()

    gone_cm = MagicMock()
    gone_cm.__enter__ = MagicMock(side_effect=CloneRefNotFoundError("gone", "r/p"))
    gone_cm.__exit__ = MagicMock(return_value=False)

    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x", "slug": "r/p"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None
        client.load_repo.side_effect = [gone_cm]

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({"default_branch": "main"})
            with pytest.raises(CloneRefNotFoundError):
                async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.GLOBAL, ref="gone") as _:
                    pass


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_reraises_when_missing_ref_is_the_default_branch():
    """Fallback enabled but the gone ref already IS the default branch: there is nothing to fall
    back to, so the error propagates and the clone is attempted only once."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()

    gone_cm = MagicMock()
    gone_cm.__enter__ = MagicMock(side_effect=CloneRefNotFoundError("main", "r/p"))
    gone_cm.__exit__ = MagicMock(return_value=False)

    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x", "slug": "r/p"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None
        client.load_repo.side_effect = [gone_cm]

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({"default_branch": "main"})
            with pytest.raises(CloneRefNotFoundError):
                async with set_runtime_ctx(
                    repo_id="r/p", scope=RepoScope.GLOBAL, ref="main", fallback_ref_on_missing=True
                ) as _:
                    pass

    assert client.load_repo.call_count == 1


def test_load_repo_fallback_body_raise_does_not_trigger_fallback():
    """A CloneRefNotFoundError raised by the *yielded body* must propagate, not trigger a fallback.

    @contextmanager throws a body-raised exception back at the ``yield`` via gen.throw(). The yield
    sits outside the ``except CloneRefNotFoundError`` guarding clone acquisition, so the body error
    must NOT be swallowed into a spurious second clone (which would also raise
    "generator didn't stop"). Only clone acquisition may reach the fallback.
    """
    from codebase.context import _load_repo_with_optional_fallback

    fake_repo = type("Repo", (), {})()
    good_cm = MagicMock()
    good_cm.__enter__ = MagicMock(return_value=fake_repo)
    good_cm.__exit__ = MagicMock(return_value=False)

    repo_client = MagicMock()
    repository = type("R", (), {"slug": "r/p"})()
    repo_client.load_repo.return_value = good_cm

    helper = _load_repo_with_optional_fallback(repo_client, repository, "gone", "main", fallback=True)
    with pytest.raises(CloneRefNotFoundError), helper as (repo, ref):
        assert repo is fake_repo
        assert ref == "gone"
        raise CloneRefNotFoundError("gone", "r/p")

    assert repo_client.load_repo.call_count == 1
    good_cm.__exit__.assert_called_once()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_assembles_references_for_issue_scope():
    """ctx.references carries both declared refs and the derived platform issue ref, deduped.

    Also guards against the **kwargs swallow hazard: if the explicit ``references`` parameter were
    dropped from the signature, ``declared`` would vanish into kwargs and only the derived
    gitlab-issue ref would appear — causing this assertion to fail.
    """
    from codebase.base import Issue, User
    from codebase.references import ExternalRef

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()

    issue = Issue(iid=42, title="Bug", author=User(id=1, username="u"))
    sentry_ref = ExternalRef(key="PROJ-123", provider="sentry", relation="closes")
    duplicate_issue_ref = ExternalRef(key="42", provider="gitlab-issue", relation="closes")
    declared = (sentry_ref, duplicate_issue_ref)

    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        client.get_git_egress_credential.return_value = None
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.ISSUE, issue=issue, references=declared) as ctx:
                # The derived issue ref leads so a declared duplicate can't demote the auto-close.
                assert ctx.references == (duplicate_issue_ref, sentry_ref)
