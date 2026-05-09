import pytest

from codebase.base import Scope
from codebase.context import RepoHandle, RuntimeCtx, set_runtime_ctx
from codebase.exceptions import SingleRepoRequiredError
from codebase.repo_config import RepositoryConfig


def _make_handle(repo_id="org/repo"):
    return RepoHandle(
        repo_id=repo_id,
        git_platform=object(),  # opaque; accessor tests don't dereference
        repository=object(),
        gitrepo=object(),
        config=RepositoryConfig(),
    )


def test_runtime_ctx_repoless_has_no_repos():
    ctx = RuntimeCtx(bot_username="daiv")
    assert ctx.has_repo is False
    assert ctx.repos == ()


def test_runtime_ctx_repoless_repo_accessor_raises():
    ctx = RuntimeCtx(bot_username="daiv")
    with pytest.raises(SingleRepoRequiredError) as excinfo:
        _ = ctx.repo
    assert excinfo.value.actual == 0


def test_runtime_ctx_single_repo_accessor_returns_handle():
    handle = _make_handle()
    ctx = RuntimeCtx(bot_username="daiv", repos=(handle,))
    assert ctx.has_repo is True
    assert ctx.repo is handle


def test_runtime_ctx_rejects_multi_repo_at_construction():
    """Multi-repo runs are reserved; the cap moves construction-time so a buggy
    producer fails at the source rather than at the first accessor read."""
    h1, h2 = _make_handle("a/x"), _make_handle("b/y")
    with pytest.raises(NotImplementedError):
        RuntimeCtx(bot_username="daiv", repos=(h1, h2))


def test_runtime_ctx_repos_normalised_to_tuple():
    """A list passed in becomes a tuple (frozen dataclass invariant)."""
    handle = _make_handle()
    ctx = RuntimeCtx(bot_username="daiv", repos=[handle])
    assert isinstance(ctx.repos, tuple)


def test_runtime_ctx_forwarding_properties_match_repo_handle():
    handle = _make_handle()
    ctx = RuntimeCtx(bot_username="daiv", repos=(handle,))
    assert ctx.repository is handle.repository
    assert ctx.gitrepo is handle.gitrepo
    assert ctx.git_platform is handle.git_platform
    assert ctx.config is handle.config


def test_runtime_ctx_forwarding_properties_raise_when_repoless():
    ctx = RuntimeCtx(bot_username="daiv")
    with pytest.raises(SingleRepoRequiredError):
        _ = ctx.repository
    with pytest.raises(SingleRepoRequiredError):
        _ = ctx.gitrepo
    with pytest.raises(SingleRepoRequiredError):
        _ = ctx.git_platform


def test_runtime_ctx_repoless_config_is_default_repository_config():
    ctx = RuntimeCtx(bot_username="daiv")
    assert ctx.config.context_file_name == RepositoryConfig().context_file_name


async def test_set_runtime_ctx_repoless_yields_empty_repos():
    async with set_runtime_ctx(repo_id=None, scope=Scope.GLOBAL) as ctx:
        assert ctx.has_repo is False
        assert ctx.repos == ()
        assert ctx.scope == Scope.GLOBAL
        assert ctx.config.context_file_name == RepositoryConfig().context_file_name
        assert isinstance(ctx.bot_username, str)
        assert ctx.bot_username  # non-empty even when repoless
