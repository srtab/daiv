"""Construction-time guards on ``RuntimeCtx``.

The constructor enforces ``len(repos) == 1``. A regression that loosened the
constraint would silently re-enable repoless runs (or quietly accept multi-repo
shapes before the codepath is ready for them); these tests pin the contract. It
also refuses a networked sandbox environment without the egress its session is
provisioned with, which would otherwise start that session with no network.
"""

from unittest.mock import Mock

import pytest

from codebase.context import RepoHandle, RuntimeCtx
from codebase.exceptions import SingleRepoRequiredError
from codebase.references import ExternalRef
from core.sandbox.schemas import EgressConfigRequest
from tests.unit_tests.conftest import sandbox_spec


def _make_handle() -> RepoHandle:
    return RepoHandle(
        repo_id="acme/api",
        git_platform=Mock(),
        repository=Mock(slug="acme/api"),
        gitrepo=Mock(),
        config=Mock(),
        ref="main",
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


def test_runtime_ctx_defaults_to_no_references():
    ctx = RuntimeCtx(bot_username="bot", repos=(_make_handle(),))
    assert ctx.references == ()


def test_runtime_ctx_normalises_references_to_a_tuple():
    ref = ExternalRef(key="PROJ-1", provider="jira")
    ctx = RuntimeCtx(bot_username="daiv", repos=(_make_handle(),), references=[ref])  # type: ignore[arg-type]
    assert isinstance(ctx.references, tuple)
    assert ctx.references == (ref,)


def test_runtime_ctx_rejects_a_networked_sandbox_without_its_provisioned_egress():
    with pytest.raises(ValueError, match="sandbox_egress"):
        RuntimeCtx(bot_username="daiv", repos=(_make_handle(),), sandbox=sandbox_spec(egress=EgressConfigRequest()))


@pytest.mark.parametrize(
    ("sandbox", "sandbox_egress"),
    [
        pytest.param(sandbox_spec(egress=EgressConfigRequest()), EgressConfigRequest(), id="networked"),
        pytest.param(sandbox_spec(), None, id="network-off"),
        pytest.param(sandbox_spec(base_image=None, egress=EgressConfigRequest()), None, id="disabled"),
        pytest.param(None, None, id="no-sandbox"),
    ],
)
def test_runtime_ctx_accepts_consistent_sandbox_egress(sandbox, sandbox_egress):
    ctx = RuntimeCtx(bot_username="daiv", repos=(_make_handle(),), sandbox=sandbox, sandbox_egress=sandbox_egress)

    assert ctx.sandbox_egress is sandbox_egress
