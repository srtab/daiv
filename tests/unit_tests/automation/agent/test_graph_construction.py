"""Static-source regression guards.

These tests use ``inspect.getsource`` to assert that ``graph.py`` and
``subagents.py`` always pass ``backend``, ``agent_root`` and the right
``working_dir`` to ``SandboxMiddleware`` when sandbox is enabled — the merged
middleware needs all three to mirror successful ``write_file``/``edit_file``
calls to the sandbox. They are cheap and catch the kind of merge-conflict or
refactor that silently disables sandbox sync.
"""

import inspect

import pytest

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module


def test_graph_passes_backend_agent_root_and_working_dir_to_sandbox_middleware():
    src = inspect.getsource(graph_module)
    assert "SandboxMiddleware(" in src and "backend=backend" in src, (
        "graph.py must construct SandboxMiddleware with backend"
    )
    assert "agent_root=agent_root" in src, "graph.py must pass agent_root to SandboxMiddleware"
    assert "working_dir=agent_path if ctx.has_repo else None" in src, (
        "graph.py must pass on-disk working_dir for repo-bound and None for repoless"
    )
    assert "_sandbox_enabled" in src, "graph.py must gate SandboxMiddleware on _sandbox_enabled"


def test_general_purpose_subagent_passes_backend_agent_root_and_working_dir_to_sandbox_middleware():
    src = inspect.getsource(subagents_module)
    assert "SandboxMiddleware(" in src and "backend=backend" in src, (
        "subagents.py must construct SandboxMiddleware with backend"
    )
    assert 'agent_root=f"/{agent_path.name}"' in src, "subagents.py must pass agent_root to SandboxMiddleware"
    assert "working_dir=agent_path if runtime.has_repo else None" in src, (
        "subagents.py must pass on-disk working_dir for repo-bound and None for repoless"
    )
    assert "if sandbox_enabled:" in src, "subagents.py must gate SandboxMiddleware on sandbox_enabled"


# ---------------------------------------------------------------------------
# resolve_agent_path
# ---------------------------------------------------------------------------


def test_resolve_agent_path_repoless_returns_virtual_root():
    """Repoless runs return a virtual ``/repo`` sentinel — no on-disk dir is created."""
    from automation.agent import graph as graph_mod
    from codebase.context import RuntimeCtx

    ctx = RuntimeCtx(bot_username="daiv")
    p = graph_mod.resolve_agent_path(ctx)
    assert p == graph_mod.REPOLESS_AGENT_VIRTUAL_PATH
    assert p.name == "repo"
    # The repoless sentinel must not be touched by os.* — verify it doesn't exist on disk.
    # (If it does exist somehow, we'd be back to the /tmp/daiv accumulation problem.)
    assert not p.exists() or p.is_absolute(), "repoless agent path must be a virtual /repo sentinel"


def test_repoless_namespace_factory_isolates_per_thread():
    """The StoreBackend namespace keys by thread_id so concurrent runs don't share state."""
    from automation.agent import graph as graph_mod

    ns_a = graph_mod._repoless_namespace_factory("abc-123")(None)
    ns_b = graph_mod._repoless_namespace_factory("xyz-456")(None)
    ns_a_repeat = graph_mod._repoless_namespace_factory("abc-123")(None)

    assert ns_a != ns_b
    assert ns_a == ns_a_repeat
    assert ns_a[0] == graph_mod._REPOLESS_NS_PREFIX
    assert ns_a[1] == "abc-123"


def test_repoless_namespace_factory_fallback_key():
    """Without a thread_id, the namespace factory falls back to a stable shared key."""
    from automation.agent import graph as graph_mod

    ns = graph_mod._repoless_namespace_factory(None)(None)
    assert ns == (graph_mod._REPOLESS_NS_PREFIX, "ephemeral")


# ---------------------------------------------------------------------------
# create_daiv_agent — repoless mode skips Git middleware
# ---------------------------------------------------------------------------


@pytest.mark.django_db
async def test_create_daiv_agent_repoless_skips_git_middleware(monkeypatch):
    """create_daiv_agent with a repoless ctx must NOT instantiate GitMiddleware
    or GitPlatformMiddleware."""
    from automation.agent import graph as graph_mod
    from automation.agent.mcp.toolkits import MCPToolkit
    from codebase.context import RuntimeCtx

    git_calls: list = []
    git_platform_calls: list = []

    class _GitSentinel:
        def __init__(self, *args, **kwargs):
            git_calls.append((args, kwargs))

    class _GitPlatformSentinel:
        def __init__(self, *args, **kwargs):
            git_platform_calls.append((args, kwargs))

    monkeypatch.setattr(graph_mod, "GitMiddleware", _GitSentinel)
    monkeypatch.setattr(graph_mod, "GitPlatformMiddleware", _GitPlatformSentinel)

    async def _no_op_get_tools(cls):
        return []

    monkeypatch.setattr(MCPToolkit, "get_tools", classmethod(_no_op_get_tools))

    ctx = RuntimeCtx(bot_username="daiv")
    await graph_mod.create_daiv_agent(ctx=ctx, sandbox_enabled=False)

    assert git_calls == [], "GitMiddleware must NOT be instantiated in repoless mode"
    assert git_platform_calls == [], "GitPlatformMiddleware must NOT be instantiated in repoless mode"
