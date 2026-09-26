from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from deepagents.backends.protocol import BackendProtocol

from automation.agent.graph import ALWAYS_LOADED_TOOLS, create_daiv_agent
from automation.agent.middlewares.ask_user_question import AskUserQuestionMiddleware
from automation.agent.middlewares.file_system import WORKSPACE_FENCE_PERMISSIONS, SandboxFileBackend
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from automation.agent.questions import ASK_USER_QUESTION_TOOL_NAME
from tests.unit_tests.conftest import FakeSandboxClient, bound_run_sandbox_client, sandbox_runtime


def _patches() -> dict[str, tuple[str, dict]]:
    return {
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


async def _build(*, base_image: str | None, **agent_kwargs) -> SimpleNamespace:
    """Build the agent with its collaborators stubbed and return the stubs plus the run's client."""
    run_client = FakeSandboxClient.opened()
    sandbox = sandbox_runtime(base_image=base_image)
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(f"automation.agent.graph.{target}", **kwargs))
            for name, (target, kwargs) in _patches().items()
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
        ctx.sandbox = sandbox
        ctx.config.context_file_name = "AGENTS.md"
        if sandbox.enabled:
            stack.enter_context(bound_run_sandbox_client(run_client))
        await create_daiv_agent(ctx=ctx, auto_commit_changes=False, **agent_kwargs)
    return SimpleNamespace(run_client=run_client, **mocks)


def _middleware(built: SimpleNamespace) -> list:
    return built.create_deep_agent.call_args.kwargs["middleware"]


async def test_disk_mode_builds_no_sandbox():
    """B10: with no base image, the run gets the disk backend, the workspace fence, no bash tool, and disk-mode
    skills and subagents."""
    built = await _build(base_image=None)

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


async def test_sandbox_mode_shares_one_backend_across_the_run():
    """B6: the parent's SandboxFileBackend also backs the git middleware and the general-purpose and custom
    subagents, which get the run's client too; explore gets it through the composite backend."""
    built = await _build(base_image="python:3.12")

    [sandbox_middleware] = [m for m in _middleware(built) if isinstance(m, SandboxMiddleware)]
    backend = sandbox_middleware._sandbox_backend
    assert isinstance(backend, SandboxFileBackend)
    assert built.composite_backend.call_args.kwargs["default"] is backend
    assert built.create_explore.call_args.args[0] is built.composite_backend.return_value
    assert built.git_middleware.call_args.kwargs["sandbox_backend"] is backend
    for kwargs in (built.create_general_purpose.call_args.kwargs, built.load_custom.await_args.kwargs):
        assert kwargs["sandbox_backend"] is backend
        assert kwargs["client"] is built.run_client
    assert built.run_client.calls == []


def test_ask_user_question_is_always_loaded():
    assert ASK_USER_QUESTION_TOOL_NAME in ALWAYS_LOADED_TOOLS


async def test_ask_user_is_enabled_by_default():
    built = await _build(base_image=None)

    [middleware] = [m for m in _middleware(built) if isinstance(m, AskUserQuestionMiddleware)]
    assert middleware.enabled is True


async def test_ask_user_can_be_disabled_for_the_run():
    built = await _build(base_image=None, ask_user_enabled=False)

    [middleware] = [m for m in _middleware(built) if isinstance(m, AskUserQuestionMiddleware)]
    assert middleware.enabled is False
