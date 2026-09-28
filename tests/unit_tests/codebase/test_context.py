from contextlib import contextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from automation.agent.middlewares.file_system import SandboxFileBackend
from automation.agent.middlewares.sandbox import SandboxMiddleware
from codebase.base import Scope as RepoScope
from codebase.clients.base import GitEgressCredential
from codebase.context import set_runtime_ctx
from codebase.exceptions import CloneRefNotFoundError
from core.sandbox.client import _run_sandbox_client, get_run_sandbox_client
from core.sandbox.egress import PLATFORM_EGRESS_SECRET_NAME
from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret
from tests.unit_tests.conftest import FakeSandboxClient, sandbox_spec


async def test_set_runtime_ctx_reads_no_sandbox_environment():
    """The run's sandbox is the spec it is handed: no environment lookup, so this test needs no database."""
    spec = sandbox_spec(base_image=None)

    with _context_deps():
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=spec) as ctx:
            assert ctx.sandbox is spec


def _repo_client(credential: GitEgressCredential | None = None, working_dir: str = "/tmp/repo"):  # noqa: S108
    repo_client = MagicMock()
    repo_client.current_user.username = "daiv"
    repo_client.load_repo.return_value = nullcontext(MagicMock(working_dir=working_dir))
    repo_client.get_git_egress_credential.return_value = credential
    return repo_client


def _context_deps(repo_client=None):
    """Patch ``set_runtime_ctx`` to load ``repo_client``'s repo."""
    return patch.multiple(
        "codebase.context",
        RepoClient=MagicMock(create_instance=MagicMock(return_value=repo_client or _repo_client())),
        RepositoryConfig=MagicMock(get_config=MagicMock(return_value=MagicMock(default_branch="main"))),
    )


async def test_set_runtime_ctx_opens_and_closes_transport_when_sandbox_enabled():
    fake_client = MagicMock()
    fake_client.open = AsyncMock(return_value=fake_client)
    fake_client.close = AsyncMock()
    with _context_deps(), patch("codebase.context.DAIVSandboxClient", return_value=fake_client):
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()):
            assert _run_sandbox_client.get() is fake_client
        fake_client.open.assert_awaited_once()
        fake_client.close.assert_awaited_once()
        assert _run_sandbox_client.get() is None


async def test_set_runtime_ctx_skips_transport_when_sandbox_disabled():
    with _context_deps(), patch("codebase.context.DAIVSandboxClient") as ctor:
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec(base_image=None)):
            assert _run_sandbox_client.get() is None
        ctor.assert_not_called()


@contextmanager
def _sandbox_run(credential: GitEgressCredential | None, working_dir: str = "/tmp/repo"):  # noqa: S108
    """Patch ``set_runtime_ctx``'s collaborators for a run over a fake sandbox transport; yield the repo client."""
    repo_client = _repo_client(credential, working_dir)
    with _context_deps(repo_client), patch("codebase.context.DAIVSandboxClient", FakeSandboxClient):
        yield repo_client


async def test_set_runtime_ctx_injects_platform_egress_when_network_on():
    """Network-on integration seam: set_runtime_ctx must resolve the repo's git-platform credential
    via the client and land its allow-rule first on ``ctx.sandbox_egress`` (the only place all the
    unit-tested egress pieces are wired together). Guards against that line being dropped or
    reordered — a regression every isolated unit test would miss."""
    credential = GitEgressCredential.for_token(host="github.com", token="tok")  # noqa: S106

    with _sandbox_run(credential) as repo_client:
        async with set_runtime_ctx(
            "acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec(egress=EgressConfigRequest())
        ) as ctx:
            repo_client.get_git_egress_credential.assert_called_once_with(repo_client.get_repository.return_value)
            assert ctx.sandbox_egress is not None
            platform_rule = ctx.sandbox_egress.policy.rules[0]
            assert platform_rule.host == "github.com"
            assert platform_rule.inject == PLATFORM_EGRESS_SECRET_NAME
            assert PLATFORM_EGRESS_SECRET_NAME in ctx.sandbox_egress.secrets
            assert ctx.sandbox.egress == EgressConfigRequest()


async def test_set_runtime_ctx_resolves_platform_egress_after_clone():
    """Regression guard for the egress/clone token-divergence bug: the git-platform credential must be
    resolved AFTER load_repo() clones, so it observes any token the GitLab clone self-heal re-minted.
    The egress proxy overrides Authorization on every platform request, so a credential captured before
    a self-healing clone would pin the sidecar to the stale token the clone just discarded — breaking
    the in-sandbox push. Asserts clone happens before credential resolution."""
    calls: list[str] = []
    cm = MagicMock()
    cm.__enter__ = MagicMock(side_effect=lambda: calls.append("clone") or MagicMock(working_dir="/tmp/repo"))  # noqa: S108
    cm.__exit__ = MagicMock(return_value=False)

    def _cred(repo):
        calls.append("credential")
        return GitEgressCredential.for_token(host="github.com", token="tok")  # noqa: S106

    # Network-off env: a push token still opens it for the git platform host.
    with _sandbox_run(None) as repo_client:
        repo_client.load_repo.return_value = cm
        repo_client.get_git_egress_credential.side_effect = _cred
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()) as ctx:
            assert calls == ["clone", "credential"], f"credential must resolve after clone, got {calls}"
            assert ctx.sandbox_egress is not None
            assert ctx.sandbox_egress.policy.rules[0].host == "github.com"


@pytest.mark.parametrize("token", [pytest.param("tok", id="push-token"), pytest.param(None, id="token-less")])
async def test_set_runtime_ctx_opens_a_network_off_env_only_for_a_push_token(token):
    """B9: a network-off env reaches the git host only when a real push token exists."""
    credential = GitEgressCredential.for_token(host="github.com", token=token)

    with _sandbox_run(credential):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()) as ctx:
            egress = ctx.sandbox_egress

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

    with _sandbox_run(credential, working_dir=str(tmp_path)):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()) as ctx:
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


@pytest.mark.parametrize(
    ("credential", "hosts"),
    [
        pytest.param(None, ["api.example.com"], id="no-credential"),
        pytest.param(GitEgressCredential(host="github.com"), ["github.com", "api.example.com"], id="token-less"),
    ],
)
async def test_set_runtime_ctx_keeps_a_network_on_env_open_without_a_push_token(credential, hosts):
    """Without a push token a network-on env keeps its rules, behind the git host when a credential names one."""
    env = sandbox_spec(egress=EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host="api.example.com")])))

    with _sandbox_run(credential):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=env) as ctx:
            egress = ctx.sandbox_egress

    assert [rule.host for rule in egress.policy.rules] == hosts
    assert PLATFORM_EGRESS_SECRET_NAME not in egress.secrets
    assert ctx.sandbox is env


async def test_set_runtime_ctx_keeps_a_network_off_env_isolated_without_a_credential():
    """B9: no derivable credential (e.g. a clone URL without a host) leaves a network-off env with no network."""
    with _sandbox_run(None):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()) as ctx:
            assert ctx.sandbox_egress is None


async def test_set_runtime_ctx_mints_nothing_for_a_disabled_sandbox():
    """A run without a sandbox has no egress to provision, so it never mints a platform token."""
    credential = GitEgressCredential.for_token(host="github.com", token="tok")  # noqa: S106
    disabled = sandbox_spec(base_image=None, egress=EgressConfigRequest())

    with _sandbox_run(credential) as repo_client:
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=disabled) as ctx:
            assert ctx.sandbox_egress is None

    repo_client.get_git_egress_credential.assert_not_called()


async def test_the_run_spec_never_carries_the_platform_token():
    """Two runs of one env mint different tokens but share the env's spec unchanged."""
    env = sandbox_spec(egress=EgressConfigRequest())
    specs, egresses = [], []
    for token in ("tok-1", "tok-2"):
        credential = GitEgressCredential.for_token(host="github.com", token=token)
        with _sandbox_run(credential):
            async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=env) as ctx:
                specs.append(ctx.sandbox)
                egresses.append(ctx.sandbox_egress)

    assert specs == [env, env]
    assert egresses[0] != egresses[1]


@pytest.mark.asyncio
async def test_set_runtime_ctx_falls_back_to_default_when_ref_missing():
    """With fallback enabled, a gone ref retries the clone on the default branch and records it."""
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
                repo_id="r/p",
                scope=RepoScope.GLOBAL,
                ref="gone",
                fallback_ref_on_missing=True,
                sandbox_spec=sandbox_spec(base_image=None),
            ) as ctx:
                assert ctx.repo.ref == "main"

    assert client.load_repo.call_count == 2
    assert client.load_repo.call_args_list[0].kwargs["sha"] == "gone"
    assert client.load_repo.call_args_list[1].kwargs["sha"] == "main"


@pytest.mark.asyncio
async def test_set_runtime_ctx_reraises_missing_ref_without_fallback():
    """Default behavior (fallback disabled) propagates CloneRefNotFoundError unchanged."""
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
                async with set_runtime_ctx(
                    repo_id="r/p", scope=RepoScope.GLOBAL, ref="gone", sandbox_spec=sandbox_spec(base_image=None)
                ) as _:
                    pass


@pytest.mark.asyncio
async def test_set_runtime_ctx_reraises_when_missing_ref_is_the_default_branch():
    """Fallback enabled but the gone ref already IS the default branch: there is nothing to fall
    back to, so the error propagates and the clone is attempted only once."""
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
                    repo_id="r/p",
                    scope=RepoScope.GLOBAL,
                    ref="main",
                    fallback_ref_on_missing=True,
                    sandbox_spec=sandbox_spec(base_image=None),
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


@pytest.mark.asyncio
async def test_set_runtime_ctx_assembles_references_for_issue_scope():
    """ctx.references carries both declared refs and the derived platform issue ref, deduped.

    Also guards against the **kwargs swallow hazard: if the explicit ``references`` parameter were
    dropped from the signature, ``declared`` would vanish into kwargs and only the derived
    gitlab-issue ref would appear — causing this assertion to fail.
    """
    from codebase.base import Issue, User
    from codebase.references import ExternalRef

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
            async with set_runtime_ctx(
                repo_id="r/p",
                scope=RepoScope.ISSUE,
                issue=issue,
                references=declared,
                sandbox_spec=sandbox_spec(base_image=None),
            ) as ctx:
                # The derived issue ref leads so a declared duplicate can't demote the auto-close.
                assert ctx.references == (duplicate_issue_ref, sentry_ref)
