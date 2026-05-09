"""Static-source regression guards.

These tests use ``inspect.getsource`` to assert that ``graph.py`` and
``subagents.py`` always pass ``backend`` and ``working_dir`` to
``SandboxMiddleware`` when sandbox is enabled — the merged middleware needs
both to mirror successful ``write_file``/``edit_file`` calls to the sandbox.
They are cheap and catch the kind of merge-conflict or refactor that silently
disables sandbox sync.
"""

import inspect

import pytest

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module


def test_graph_passes_backend_and_working_dir_to_sandbox_middleware():
    src = inspect.getsource(graph_module)
    assert "SandboxMiddleware(backend=backend, working_dir=agent_path)" in src, (
        "graph.py must construct SandboxMiddleware with backend and working_dir"
    )
    assert "_sandbox_enabled" in src, "graph.py must gate SandboxMiddleware on _sandbox_enabled"


def test_general_purpose_subagent_passes_backend_and_working_dir_to_sandbox_middleware():
    src = inspect.getsource(subagents_module)
    assert "SandboxMiddleware(" in src and "backend=backend" in src, (
        "subagents.py must construct SandboxMiddleware with backend and working_dir"
    )
    assert "working_dir=sub_working_dir" in src, (
        "subagents.py must pass working_dir to SandboxMiddleware via sub_working_dir"
    )
    assert "if sandbox_enabled:" in src, "subagents.py must gate SandboxMiddleware on sandbox_enabled"


# ---------------------------------------------------------------------------
# resolve_agent_path
# ---------------------------------------------------------------------------


def test_resolve_agent_path_repoless_uses_ephemeral_dir(tmp_path, monkeypatch):
    from automation.agent import graph as graph_mod
    from codebase.context import RuntimeCtx
    from codebase.repo_config import RepositoryConfig

    monkeypatch.setattr(graph_mod, "_REPOLESS_AGENT_PATH_ROOT", str(tmp_path))
    ctx = RuntimeCtx(bot_username="daiv", repos=[], scope=None, config=RepositoryConfig())
    p = graph_mod.resolve_agent_path(ctx, thread_id="abc-123")
    assert str(p).startswith(str(tmp_path))
    assert p.name == "repo"
    assert "abc-123" in str(p.parent)


def test_resolve_agent_path_repoless_creates_directory(tmp_path, monkeypatch):
    from automation.agent import graph as graph_mod
    from codebase.context import RuntimeCtx
    from codebase.repo_config import RepositoryConfig

    monkeypatch.setattr(graph_mod, "_REPOLESS_AGENT_PATH_ROOT", str(tmp_path))
    ctx = RuntimeCtx(bot_username="daiv", repos=[], scope=None, config=RepositoryConfig())
    p = graph_mod.resolve_agent_path(ctx, thread_id="xyz-456")
    assert p.exists()
    assert p.is_dir()


def test_resolve_agent_path_repoless_fallback_key(tmp_path, monkeypatch):
    """When no thread_id is given, falls back to 'ephemeral' as directory key."""
    from automation.agent import graph as graph_mod
    from codebase.context import RuntimeCtx
    from codebase.repo_config import RepositoryConfig

    monkeypatch.setattr(graph_mod, "_REPOLESS_AGENT_PATH_ROOT", str(tmp_path))
    ctx = RuntimeCtx(bot_username="daiv", repos=[], scope=None, config=RepositoryConfig())
    p = graph_mod.resolve_agent_path(ctx)
    assert "ephemeral" in str(p)


# ---------------------------------------------------------------------------
# create_daiv_agent — repoless mode skips Git middleware
# ---------------------------------------------------------------------------


@pytest.mark.django_db
async def test_create_daiv_agent_repoless_skips_git_middleware(monkeypatch, tmp_path):
    """create_daiv_agent with a repoless ctx must NOT instantiate GitMiddleware
    or GitPlatformMiddleware."""
    from automation.agent import graph as graph_mod
    from automation.agent.mcp.toolkits import MCPToolkit
    from codebase.context import RuntimeCtx
    from codebase.repo_config import RepositoryConfig

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
    monkeypatch.setattr(graph_mod, "_REPOLESS_AGENT_PATH_ROOT", str(tmp_path))

    async def _no_op_get_tools(cls):
        return []

    monkeypatch.setattr(MCPToolkit, "get_tools", classmethod(_no_op_get_tools))

    ctx = RuntimeCtx(bot_username="daiv", repos=[], scope=None, config=RepositoryConfig())
    await graph_mod.create_daiv_agent(ctx=ctx, sandbox_enabled=False)

    assert git_calls == [], "GitMiddleware must NOT be instantiated in repoless mode"
    assert git_platform_calls == [], "GitPlatformMiddleware must NOT be instantiated in repoless mode"
