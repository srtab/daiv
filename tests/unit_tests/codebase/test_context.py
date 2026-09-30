from contextlib import contextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from git import Repo

from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.workspace.sandbox import SandboxWorkspace
from automation.agent.workspace.session import SandboxSession
from codebase.base import Scope as RepoScope
from codebase.clients.base import GitEgressCredential
from codebase.context import set_runtime_ctx
from codebase.exceptions import CloneRefNotFoundError
from core.sandbox.schemas import EgressConfigRequest, EgressSecret
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
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()) as ctx:
            assert ctx.sandbox_client is fake_client
            fake_client.close.assert_not_awaited()
        fake_client.open.assert_awaited_once()
        fake_client.close.assert_awaited_once()


async def test_set_runtime_ctx_skips_transport_when_sandbox_disabled():
    with _context_deps(), patch("codebase.context.DAIVSandboxClient") as ctor:
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec(base_image=None)) as ctx:
            assert ctx.sandbox_client is None
        ctor.assert_not_called()


@contextmanager
def _sandbox_run(credential: GitEgressCredential | None, working_dir: str = "/tmp/repo"):  # noqa: S108
    """Patch ``set_runtime_ctx``'s collaborators for a run over a fake sandbox transport; yield the repo client."""
    repo_client = _repo_client(credential, working_dir)
    with _context_deps(repo_client), patch("codebase.context.DAIVSandboxClient", FakeSandboxClient):
        yield repo_client


def _clone_on_main(path) -> Repo:
    """A real repo with one commit on ``main``, standing in for what ``load_repo`` yields."""
    repo = Repo.init(path)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "test@example.com")
    (path / "README.md").write_text("x\n")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    repo.git.branch("-M", "main")
    return repo


def _loading(repo: Repo):
    repo_client = _repo_client()
    repo_client.load_repo.return_value = nullcontext(repo)
    return _context_deps(repo_client)


async def test_the_handle_records_the_branch_the_clone_is_on(tmp_path):
    with _loading(_clone_on_main(tmp_path)):
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec(base_image=None)) as ctx:
            assert (ctx.repo.current_ref, ctx.repo.head_detached) == ("main", False)


async def test_the_handle_records_a_tag_clone_by_its_commit(tmp_path):
    """A tag detaches HEAD, so no branch names it: the handle carries the commit and says it is detached."""
    repo = _clone_on_main(tmp_path)
    repo.create_tag("v1.0")
    repo.git.checkout("--detach", "v1.0")

    with _loading(repo):
        async with set_runtime_ctx(
            "repo-1", scope=RepoScope.GLOBAL, ref="v1.0", sandbox_spec=sandbox_spec(base_image=None)
        ) as ctx:
            assert (ctx.repo.current_ref, ctx.repo.head_detached) == (repo.head.commit.hexsha, True)
            assert ctx.repo.ref == "v1.0"


async def test_the_handle_keeps_the_ref_the_clone_was_made_on(tmp_path):
    """Recorded once: a sandbox run's git runs in the sandbox, so the worker clone's HEAD is not where the run is."""
    repo = _clone_on_main(tmp_path)

    with _loading(repo):
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec(base_image=None)) as ctx:
            repo.git.checkout("-b", "elsewhere")
            assert ctx.repo.current_ref == "main"


async def test_the_credential_source_mints_after_the_clone():
    """The session mints through the context's credential source, which exists only once the clone is done: the
    egress proxy then injects any token the clone's self-heal re-minted, never the stale one it discarded."""
    calls: list[str] = []
    cm = MagicMock()
    cm.__enter__ = MagicMock(side_effect=lambda: calls.append("clone") or MagicMock(working_dir="/tmp/repo"))  # noqa: S108
    cm.__exit__ = MagicMock(return_value=False)
    credential = GitEgressCredential.for_token(host="github.com", token="tok")  # noqa: S106

    with _sandbox_run(None) as repo_client:
        repo_client.load_repo.return_value = cm
        repo_client.get_git_egress_credential.side_effect = lambda _repo: calls.append("credential") or credential
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()) as ctx:
            assert calls == ["clone"]
            assert await ctx.credential_source() is credential

    assert calls == ["clone", "credential"]
    repo_client.get_git_egress_credential.assert_called_once_with(repo_client.get_repository.return_value)


@pytest.mark.parametrize("token", [pytest.param("tok", id="push-token"), pytest.param(None, id="token-less")])
async def test_a_network_off_sandbox_session_reaches_the_git_host_only_for_a_push_token(token, tmp_path):
    """B9, end to end: the session a run builds from its context starts a network-off env with egress only for the
    git host, and only with a token."""
    (tmp_path / "README.md").write_text("hello\n")
    credential = GitEgressCredential.for_token(host="github.com", token=token)

    with _sandbox_run(credential, working_dir=str(tmp_path)):
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec()) as ctx:
            client = ctx.sandbox_client
            session = SandboxSession(client, ctx.sandbox, credential_source=ctx.credential_source)
            middleware = SandboxMiddleware(agent_root="/workspace/repo", workspace=SandboxWorkspace(session))
            await middleware.abefore_agent({}, MagicMock(context=ctx))
            await session.release(resumable=True)

    assert not client.is_open
    [session] = client.sessions.values()
    egress = session.request.egress
    if token:
        [rule] = egress.policy.rules
        assert (egress.policy.default, rule.host) == ("deny", "github.com")
        assert egress.secrets == {rule.inject: EgressSecret(header=credential.header, value=credential.value)}
    else:
        assert egress is None


async def test_set_runtime_ctx_mints_nothing_for_a_disabled_sandbox():
    """A run without a sandbox has no egress to provision, so it never mints a platform token."""
    credential = GitEgressCredential.for_token(host="github.com", token="tok")  # noqa: S106
    disabled = sandbox_spec(base_image=None, egress=EgressConfigRequest())

    with _sandbox_run(credential) as repo_client:
        async with set_runtime_ctx("acme/repo", scope=RepoScope.GLOBAL, sandbox_spec=disabled) as ctx:
            assert (ctx.sandbox_client, ctx.credential_source) == (None, None)

    repo_client.get_git_egress_credential.assert_not_called()


@pytest.mark.asyncio
async def test_set_runtime_ctx_falls_back_to_default_when_ref_missing():
    """With fallback enabled, a gone ref retries the clone on the default branch and records it."""
    fake_repo = MagicMock()
    fake_repo.active_branch.name = "main"
    fake_repo.head.is_detached = False
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
                assert ctx.repo.current_ref == "main"

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
        ctx_mgr.__enter__.return_value = MagicMock()
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


async def test_the_handle_records_how_long_the_clone_took():
    with _context_deps(), patch("codebase.context.monotonic", side_effect=[100.0, 102.5]):
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL, sandbox_spec=sandbox_spec(base_image=None)) as ctx:
            assert ctx.repo.clone_seconds == 2.5


async def test_a_fallback_clone_is_timed_across_both_attempts():
    """Two clock reads around both clone attempts: the vanished ref's failed clone counts too."""
    gone = MagicMock()
    gone.__enter__ = MagicMock(side_effect=CloneRefNotFoundError("gone", "r/p"))
    repo_client = _repo_client()
    repo_client.load_repo.side_effect = [gone, nullcontext(MagicMock())]

    with _context_deps(repo_client), patch("codebase.context.monotonic", side_effect=[100.0, 104.0]):
        async with set_runtime_ctx(
            "repo-1",
            scope=RepoScope.GLOBAL,
            ref="gone",
            fallback_ref_on_missing=True,
            sandbox_spec=sandbox_spec(base_image=None),
        ) as ctx:
            assert (ctx.repo.ref, ctx.repo.clone_seconds) == ("main", 4.0)

    assert repo_client.load_repo.call_count == 2
