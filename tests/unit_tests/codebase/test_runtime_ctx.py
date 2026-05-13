"""Construction-time guards on ``RuntimeCtx``.

The constructor enforces ``len(repos) == 1``. A regression that loosened the
constraint would silently re-enable repoless runs (or quietly accept multi-repo
shapes before the codepath is ready for them); these tests pin the contract.
"""

from unittest.mock import Mock

import pytest

from codebase.context import RepoHandle, RuntimeCtx
from codebase.exceptions import SingleRepoRequiredError


def _make_handle() -> RepoHandle:
    return RepoHandle(
        repo_id="acme/api", git_platform=Mock(), repository=Mock(slug="acme/api"), gitrepo=Mock(), config=Mock()
    )


def test_runtime_ctx_accepts_exactly_one_repo():
    handle = _make_handle()
    ctx = RuntimeCtx(bot_username="daiv", repos=(handle,))
    assert ctx.repo is handle
    assert ctx.repository is handle.repository
    assert ctx.gitrepo is handle.gitrepo
    assert ctx.config is handle.config


def test_runtime_ctx_rejects_zero_repos():
    with pytest.raises(SingleRepoRequiredError) as exc:
        RuntimeCtx(bot_username="daiv", repos=())
    assert exc.value.actual == 0
    assert "0" in str(exc.value)


def test_runtime_ctx_rejects_multiple_repos():
    with pytest.raises(SingleRepoRequiredError) as exc:
        RuntimeCtx(bot_username="daiv", repos=(_make_handle(), _make_handle()))
    assert exc.value.actual == 2
    assert "multi-repo" in str(exc.value)


def test_runtime_ctx_normalises_list_to_tuple():
    """``__post_init__`` coerces non-tuple iterables so the frozen dataclass stays hashable."""
    handle = _make_handle()
    ctx = RuntimeCtx(bot_username="daiv", repos=[handle])  # type: ignore[arg-type]
    assert isinstance(ctx.repos, tuple)
    assert ctx.repos == (handle,)
