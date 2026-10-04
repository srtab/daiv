from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain.agents.middleware import ModelRequest, ModelResponse

from automation.agent.artifacts import PUBLISH_ARTIFACT_TOOL_NAME
from automation.agent.graph import ALWAYS_LOADED_TOOLS, create_daiv_agent, dynamic_daiv_system_prompt
from automation.agent.middlewares.artifacts import ArtifactsMiddleware
from automation.agent.middlewares.ask_user_question import AskUserQuestionMiddleware
from automation.agent.middlewares.file_system import WORKSPACE_FENCE_PERMISSIONS, DAIVFilesystemMiddleware
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from automation.agent.questions import ASK_USER_QUESTION_TOOL_NAME
from automation.agent.workspace.disk import DiskWorkspace
from automation.agent.workspace.sandbox import SandboxWorkspace
from automation.agent.workspace.session import SandboxSession
from codebase.base import GitPlatform
from tests.unit_tests.conftest import FakeArtifactStore, FakeSandboxClient, agent_settings, sandbox_spec, site_snapshot


def _patches() -> dict[str, tuple[str, dict]]:
    return {
        "create_general_purpose": ("create_general_purpose_subagent", {}),
        "create_explore": ("create_explore_subagent", {}),
        "load_custom": ("load_custom_subagents", {"new": AsyncMock(return_value=[])}),
        "create_deep_agent": ("create_deep_agent", {}),
        "mcp_toolkit": ("MCPToolkit", {"get_tools": AsyncMock(return_value=[])}),
        "base_agent": ("BaseAgent", {}),
        "skills_middleware": ("SkillsMiddleware", {}),
        "git_middleware": ("GitMiddleware", {}),
        "git_platform_middleware": ("GitPlatformMiddleware", {}),
        "prompt_caching_middleware": ("AnthropicPromptCachingMiddleware", {}),
        "tool_call_logging_middleware": ("ToolCallLoggingMiddleware", {}),
    }


async def _build(workspace, *, load_custom_subagents: bool = False, **agent_kwargs) -> SimpleNamespace:
    """Build the agent over ``workspace`` with its collaborators stubbed and return the stubs. The context's spec has a
    base image, so a disk workspace shows the mode is the workspace's, not the context's. ``load_custom_subagents``
    runs the real custom-subagent loader instead of its stub."""
    patches = _patches()
    if load_custom_subagents:
        del patches["load_custom"]
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(f"automation.agent.graph.{target}", **kwargs))
            for name, (target, kwargs) in patches.items()
        }
        stack.enter_context(patch("automation.agent.middlewares.deferred_tools.deferred_settings", ENABLED=False))
        site = site_snapshot(
            agent_recursion_limit=50,
            agent_model_name="m",
            agent_fallback_model_name="m",
            agent_thinking_level=None,
            web_fetch_enabled=False,
            web_search_enabled=False,
        )
        ctx = MagicMock()
        ctx.sandbox = sandbox_spec()
        ctx.config.context_file_name = "AGENTS.md"
        await create_daiv_agent(
            settings=agent_settings(site=site), ctx=ctx, workspace=workspace, auto_commit_changes=False, **agent_kwargs
        )
    return SimpleNamespace(**mocks)


def _disk_workspace() -> DiskWorkspace:
    ctx = MagicMock()
    ctx.gitrepo.working_dir = "/repo"
    return DiskWorkspace(ctx)


def _sandbox_workspace(clone: Path = Path("/repo")) -> tuple[SandboxWorkspace, FakeSandboxClient]:
    client = FakeSandboxClient.opened()
    return SandboxWorkspace(SandboxSession(client, sandbox_spec()), clone=clone), client


def _middleware(built: SimpleNamespace) -> list:
    return built.create_deep_agent.call_args.kwargs["middleware"]


def _filesystem_middleware(built: SimpleNamespace) -> DAIVFilesystemMiddleware:
    [middleware] = [m for m in _middleware(built) if isinstance(m, DAIVFilesystemMiddleware)]
    return middleware


async def test_disk_mode_builds_no_sandbox():
    """B10: a disk workspace gives the run no bash tool, the workspace fence, and copied global skills."""
    workspace = _disk_workspace()
    built = await _build(workspace)

    deep_agent_kwargs = built.create_deep_agent.call_args.kwargs
    assert deep_agent_kwargs["backend"] is workspace.backend
    assert deep_agent_kwargs["permissions"] == WORKSPACE_FENCE_PERMISSIONS
    assert _filesystem_middleware(built)._permissions == WORKSPACE_FENCE_PERMISSIONS
    assert not any(isinstance(m, SandboxMiddleware) for m in _middleware(built))
    assert not any(t.name == BASH_TOOL_NAME for m in _middleware(built) for t in getattr(m, "tools", None) or [])
    assert built.skills_middleware.call_args.kwargs["copy_global_skills"] is True
    assert built.git_middleware.call_args.kwargs["workspace"] is workspace
    assert built.create_general_purpose.call_args.args[1] is workspace
    assert built.create_explore.call_args.args[0] is workspace
    assert built.load_custom.await_args.kwargs["workspace"] is workspace


async def test_sandbox_mode_shares_one_workspace_across_the_run():
    """B6: the main agent's sandbox middleware and every subagent builder get the run's one workspace, so their files,
    shell and git all reach its one session."""
    workspace, client = _sandbox_workspace()
    built = await _build(workspace, artifact_store=FakeArtifactStore())

    [sandbox_middleware] = [m for m in _middleware(built) if isinstance(m, SandboxMiddleware)]
    assert (sandbox_middleware._bash, sandbox_middleware._session) == (workspace.bash, workspace.session)
    assert built.create_deep_agent.call_args.kwargs["backend"] is workspace.backend
    assert built.create_deep_agent.call_args.kwargs["permissions"] is None
    assert _filesystem_middleware(built)._permissions == []
    assert built.create_general_purpose.call_args.args[1] is workspace
    assert built.create_explore.call_args.args[0] is workspace
    assert built.load_custom.await_args.kwargs["workspace"] is workspace
    assert built.git_middleware.call_args.kwargs["workspace"] is workspace
    assert built.skills_middleware.call_args.kwargs["copy_global_skills"] is False
    [artifacts] = [m for m in _middleware(built) if isinstance(m, ArtifactsMiddleware)]
    assert artifacts._workspace is workspace
    assert client.calls == []


async def test_sandbox_mode_loads_the_repos_custom_subagents_before_the_session_is_acquired(tmp_path):
    """The agent is built before ``SandboxMiddleware`` acquires the session, so the definitions are read from the
    clone that seeds the sandbox, and nothing reaches the sandbox while building."""
    subagents_dir = tmp_path / ".agents" / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "reviewer.md").write_text("---\nname: reviewer\ndescription: Reviews changes\n---\nYou review.\n")
    workspace, client = _sandbox_workspace(clone=tmp_path)

    built = await _build(workspace, load_custom_subagents=True)

    names = [subagent["name"] for subagent in built.create_deep_agent.call_args.kwargs["subagents"]]
    assert "reviewer" in names
    assert workspace.is_ready is False
    assert client.calls == []


async def test_the_artifact_store_reaches_the_publish_tool():
    store = FakeArtifactStore()
    built = await _build(_disk_workspace(), artifact_store=store)

    [artifacts] = [m for m in _middleware(built) if isinstance(m, ArtifactsMiddleware)]
    assert artifacts._store is store


async def test_without_an_artifact_store_the_agent_has_no_publish_tool():
    built = await _build(_disk_workspace())

    tools = [t.name for m in _middleware(built) for t in getattr(m, "tools", None) or []]
    assert not any(isinstance(m, ArtifactsMiddleware) for m in _middleware(built))
    assert PUBLISH_ARTIFACT_TOOL_NAME not in tools


def test_ask_user_question_is_always_loaded():
    assert ASK_USER_QUESTION_TOOL_NAME in ALWAYS_LOADED_TOOLS


async def test_ask_user_is_enabled_by_default():
    built = await _build(_disk_workspace())

    assert any(isinstance(m, AskUserQuestionMiddleware) for m in _middleware(built))


async def test_ask_user_can_be_disabled_for_the_run():
    built = await _build(_disk_workspace(), ask_user_enabled=False)

    assert not any(isinstance(m, AskUserQuestionMiddleware) for m in _middleware(built))


async def test_system_prompt_names_the_ref_recorded_on_the_repo_handle():
    context = SimpleNamespace(
        bot_username="daiv",
        repository=SimpleNamespace(html_url="https://gitlab.test/group/repo"),
        git_platform=GitPlatform.GITLAB,
        repo=SimpleNamespace(current_ref="feature-x"),
    )
    request = ModelRequest(
        model=MagicMock(), messages=[], system_prompt=None, state=MagicMock(), runtime=SimpleNamespace(context=context)
    )
    seen: list[str | None] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        seen.append(req.system_prompt)
        return ModelResponse(result=[])

    await dynamic_daiv_system_prompt.awrap_model_call(request, handler)

    assert "You are on branch `feature-x`" in seen[0]
    assert "https://gitlab.test/group/repo/-/blob/feature-x/" in seen[0]
