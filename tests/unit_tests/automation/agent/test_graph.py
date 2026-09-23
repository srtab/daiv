from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from deepagents.backends.protocol import BackendProtocol

from automation.agent.graph import create_daiv_agent
from automation.agent.middlewares.file_system import WORKSPACE_FENCE_PERMISSIONS, SandboxFileBackend
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from core.sandbox.client import reset_run_sandbox_client, set_run_sandbox_client
from tests.unit_tests.conftest import FakeSandboxClient

_PATCHES = {
    "disk_backend": ("build_disk_workspace_backend", {"return_value": MagicMock(spec=BackendProtocol)}),
    "composite_backend": ("DAIVCompositeBackend", {"return_value": MagicMock(spec=BackendProtocol)}),
    "create_general_purpose": ("create_general_purpose_subagent", {}),
    "create_explore": ("create_explore_subagent", {}),
    "load_custom": ("load_custom_subagents", {"new": AsyncMock(return_value=[])}),
    "create_deep_agent": ("create_deep_agent", {}),
    "mcp_toolkit": ("MCPToolkit", {"get_tools": AsyncMock(return_value=[])}),
    "base_agent": ("BaseAgent", {}),
    "site_settings": ("site_settings", {}),
    "skills_middleware": ("SkillsMiddleware", {}),
    "git_middleware": ("GitMiddleware", {}),
    "git_platform_middleware": ("GitPlatformMiddleware", {}),
    "prompt_caching_middleware": ("AnthropicPromptCachingMiddleware", {}),
    "tool_call_logging_middleware": ("ToolCallLoggingMiddleware", {}),
}


async def _build(*, sandbox_enabled: bool) -> SimpleNamespace:
    """Build the agent with every collaborator stubbed and return the stubs plus the run's client."""
    run_client = FakeSandboxClient()
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(f"automation.agent.graph.{target}", **kwargs))
            for name, (target, kwargs) in _PATCHES.items()
        }
        stack.enter_context(patch("automation.agent.middlewares.deferred_tools.deferred_settings", ENABLED=False))
        mocks["site_settings"].configure_mock(
            agent_recursion_limit=50,
            agent_model_name="m",
            agent_fallback_model_name="m",
            agent_thinking_level=None,
            web_fetch_enabled=False,
            web_search_enabled=False,
        )
        ctx = MagicMock()
        ctx.gitrepo.working_dir = "/repo"
        ctx.sandbox.enabled = sandbox_enabled
        ctx.config.context_file_name = "AGENTS.md"
        token = set_run_sandbox_client(run_client) if sandbox_enabled else None
        try:
            await create_daiv_agent(ctx=ctx, auto_commit_changes=False)
        finally:
            if token is not None:
                reset_run_sandbox_client(token)
    return SimpleNamespace(run_client=run_client, **mocks)


def _middleware(built: SimpleNamespace) -> list:
    return built.create_deep_agent.call_args.kwargs["middleware"]


async def test_disk_mode_builds_no_sandbox():
    """B10: no base image means the disk backend, the workspace fence, no bash and copied skills."""
    built = await _build(sandbox_enabled=False)

    deep_agent_kwargs = built.create_deep_agent.call_args.kwargs
    built.disk_backend.assert_called_once_with(Path("/repo"))
    assert deep_agent_kwargs["backend"] is built.disk_backend.return_value
    assert deep_agent_kwargs["permissions"] == WORKSPACE_FENCE_PERMISSIONS
    assert not any(isinstance(m, SandboxMiddleware) for m in _middleware(built))
    assert not any(t.name == BASH_TOOL_NAME for m in _middleware(built) for t in getattr(m, "tools", None) or [])
    assert built.skills_middleware.call_args.kwargs["sandbox_enabled"] is False
    assert built.git_middleware.call_args.kwargs["sandbox_backend"] is None
    general_purpose_kwargs = built.create_general_purpose.call_args.kwargs
    assert general_purpose_kwargs["sandbox_enabled"] is False
    assert general_purpose_kwargs["client"] is None
    assert general_purpose_kwargs["sandbox_backend"] is None
    assert built.create_explore.call_args.kwargs["sandbox_enabled"] is False
    assert built.load_custom.await_args.kwargs["sandbox_enabled"] is False
    assert built.run_client.calls == []


async def test_sandbox_mode_shares_one_backend_across_the_run():
    """B6: the parent, every subagent and the git middleware get the same backend and client."""
    built = await _build(sandbox_enabled=True)

    [sandbox_middleware] = [m for m in _middleware(built) if isinstance(m, SandboxMiddleware)]
    backend = sandbox_middleware._sandbox_backend
    assert isinstance(backend, SandboxFileBackend)
    assert built.git_middleware.call_args.kwargs["sandbox_backend"] is backend
    for kwargs in (built.create_general_purpose.call_args.kwargs, built.load_custom.await_args.kwargs):
        assert kwargs["sandbox_backend"] is backend
        assert kwargs["client"] is built.run_client
    assert built.run_client.calls == []
