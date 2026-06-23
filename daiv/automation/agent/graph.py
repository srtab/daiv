import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from django.utils import timezone

from deepagents import create_deep_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    InterruptOnConfig,
    ModelFallbackMiddleware,
    ModelRequest,
    dynamic_prompt,
)

from automation.agent.base import BaseAgent, ThinkingLevel
from automation.agent.constants import (
    AGENTS_MEMORY_PATH,
    REPO_PATH,
    SKILLS_PATH,
    SKILLS_SOURCES,
    SUBAGENTS_SOURCES,
    WORKSPACE_PATH,
    ModelName,
)
from automation.agent.mcp.toolkits import MCPToolkit
from automation.agent.middlewares.deferred_tools import deferred_tools_middleware, direct_mcp_tools
from automation.agent.middlewares.ensure_response import ensure_non_empty_response
from automation.agent.middlewares.file_system import (
    WORKSPACE_FENCE_PERMISSIONS,
    DAIVCompositeBackend,
    SandboxFileBackend,
    build_disk_workspace_backend,
    filesystem_absolute_path_directive,
)
from automation.agent.middlewares.git import GitMiddleware
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.logging import ToolCallLoggingMiddleware
from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
from automation.agent.middlewares.memory import RepositoryMemoryMiddleware
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from automation.agent.middlewares.skills import SKILLS_TOOL_NAME, SkillsMiddleware
from automation.agent.middlewares.slash_commands import SlashCommandMiddleware
from automation.agent.middlewares.step_budget import StepBudgetMiddleware
from automation.agent.middlewares.todos import DAIVTodoListMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.prompts import DAIV_SYSTEM_PROMPT, REPO_RELATIVE_SYSTEM_REMINDER, WRITE_TODOS_SYSTEM_PROMPT
from automation.agent.subagents import (
    create_explore_subagent,
    create_general_purpose_subagent,
    load_builtin_code_review_detectors,
    load_custom_subagents,
)
from codebase.base import GitPlatform
from codebase.context import RuntimeCtx
from codebase.utils import get_repo_ref
from core.constants import BOT_NAME
from core.sandbox.client import get_run_sandbox_client
from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.backends.protocol import BackendProtocol
    from deepagents.middleware.filesystem import FilesystemPermission
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


logger = logging.getLogger("daiv.agent")


# Tools always bound to the model; everything else is deferred behind tool_search.
# DAIV-owned tool names reference their canonical constant (so a rename propagates here);
# deepagents/langchain-provided names (filesystem, write_todos, task) have no authoritative DAIV
# constant and stay as literals.
ALWAYS_LOADED_TOOLS = frozenset({
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    BASH_TOOL_NAME,
    "write_todos",
    SKILLS_TOOL_NAME,
    "task",
})


class _Unset:
    """Sentinel to distinguish 'not provided' from ``None`` in function defaults."""


def _output_invariants_system_prompt(working_directory: str) -> str:
    """Output invariants keyed to the run's absolute repo prefix (always ``/workspace/repo/`` —
    unified across sandbox and disk-backed runs)."""
    prefix = working_directory.rstrip("/") + "/"
    return f"""\
<output_invariants>
Applies to ALL user-visible text:

- NEVER include "{prefix}" anywhere in user-visible output.
- Any repository file path shown to the user MUST be repo-relative (no leading "/").
  <example>{prefix}daiv/core/utils.py -> daiv/core/utils.py</example>
- Code reference labels MUST be repo-relative paths (e.g. `daiv/core/utils.py:42`), but hrefs should use platform-native blob URLs with branch refs.
- Before emitting any user-visible text, check for "{prefix}" and rewrite to repo-relative form.

{filesystem_absolute_path_directive(working_directory)}
</output_invariants>"""  # noqa: E501


@dynamic_prompt
async def dynamic_daiv_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic prompt for the DAIV system.

    Args:
        request (ModelRequest): The request to the model.

    Returns:
        str: The dynamic prompt for the DAIV system.
    """
    context = cast("RuntimeCtx", request.runtime.context)
    # Unified across modes: repo files live under /workspace/repo regardless of sandbox.
    working_directory = f"{REPO_PATH}/"

    daiv_system_prompt = await DAIV_SYSTEM_PROMPT.aformat(
        current_date=timezone.now().strftime("%d %B, %Y"),
        bot_name=BOT_NAME,
        bot_username=context.bot_username,
        repository_url=context.repository.html_url,
        gitlab_platform=context.git_platform == GitPlatform.GITLAB,
        github_platform=context.git_platform == GitPlatform.GITHUB,
        bash_tool_enabled=BASH_TOOL_NAME in [tool.name for tool in request.tools],
        working_directory=working_directory,
        current_branch=get_repo_ref(context.gitrepo),
    )

    # The harness profile sets ``base_system_prompt=""`` to suppress upstream's
    # BASE_AGENT_PROMPT, but model-level profiles (e.g. anthropic:claude-opus-4-7)
    # still contribute a ``system_prompt_suffix`` we want to keep. Strip to drop
    # leading whitespace introduced by an empty base + suffix concat.
    inherited = (request.system_prompt or "").strip()
    inherited_system_prompt = f"{inherited}\n\n" if inherited else ""

    return (
        _output_invariants_system_prompt(working_directory)
        + "\n\n"
        + cast("str", daiv_system_prompt.content).strip()
        + "\n\n"
        + inherited_system_prompt
        + REPO_RELATIVE_SYSTEM_REMINDER
    )


def dynamic_write_todos_system_prompt(bash_tool_enabled: bool) -> str:
    """
    Dynamic prompt for the write todos system.
    """
    return cast("str", WRITE_TODOS_SYSTEM_PROMPT.format(bash_tool_enabled=bash_tool_enabled).content)


async def create_daiv_agent(
    model_names: Sequence[ModelName | str] | None = None,
    thinking_level: ThinkingLevel | None | type[_Unset] = _Unset,
    *,
    ctx: RuntimeCtx,
    auto_commit_changes: bool = True,
    capture_patch: bool = False,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    debug: bool = False,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    middleware: list[AgentMiddleware] | None = None,
    # Flags to override the default settings
    sandbox_enabled: bool | None = None,
    web_fetch_enabled: bool | None = None,
    web_search_enabled: bool | None = None,
):
    """
    Create the DAIV agent.

    Args:
        model_names: The model names to use for the agent.
        thinking_level: The thinking level to use for the agent.
        ctx: The runtime context.
        auto_commit_changes: Whether to commit the changes to the repository when the agent finishes.
        capture_patch: Whether to expose the run's working-tree diff as ``model_patch`` in the
            output state at turn end. For eval harnesses; keep ``False`` for normal runs.
        checkpointer: The checkpointer to use for the agent.
        store: The store to use for the agent.
        debug: Whether to enable debug mode for the agent.
        interrupt_on: The interrupt on configuration for the agent.
        middleware: The middleware to use for the agent.
        sandbox_enabled: Whether to enable the sandbox for the agent. If None, fallback to the config default.
        web_fetch_enabled: Whether to enable web fetch for the agent. If None, fallback to the config default.
        web_search_enabled: Whether to enable web search for the agent. If None, fallback to the config default.

    Returns:
        The DAIV agent.
    """
    if model_names is None:
        model_names = (site_settings.agent_model_name, site_settings.agent_fallback_model_name)
    if thinking_level is _Unset:
        thinking_level = site_settings.agent_thinking_level

    model = BaseAgent.get_model(model=model_names[0], thinking_level=thinking_level)
    # Read straight from settings — do not reuse ``thinking_level`` here, it may carry a per-turn
    # override of the primary that must not leak into the fallback.
    fallback_thinking_level = site_settings.agent_fallback_thinking_level
    fallback_models = [
        BaseAgent.get_model(model=model_name, thinking_level=fallback_thinking_level) for model_name in model_names[1:]
    ]

    _sandbox_enabled = sandbox_enabled if sandbox_enabled is not None else ctx.sandbox.enabled
    _web_fetch_enabled = web_fetch_enabled if web_fetch_enabled is not None else site_settings.web_fetch_enabled
    _web_search_enabled = web_search_enabled if web_search_enabled is not None else site_settings.web_search_enabled

    # Unified workspace namespace: the agent addresses /workspace/repo, /workspace/skills and
    # /workspace/tmp regardless of sandbox mode. Only the backend behind /workspace differs.
    agent_root = REPO_PATH
    global_skills_source = SKILLS_PATH

    sandbox_backend: SandboxFileBackend | None = None
    run_client = None
    main_agent_permissions: list[FilesystemPermission] | None = None
    if _sandbox_enabled:
        # Sandbox-authoritative: one pass-through SandboxFileBackend serves all of /workspace, bound
        # to the run's session by SandboxMiddleware.abefore_agent. It is wrapped in a composite only
        # so the offloading middlewares get an ``artifacts_root`` under /workspace (a bare backend
        # would default to "/" and write evictions outside /workspace, which the sandbox rejects).
        run_client = get_run_sandbox_client()
        sandbox_backend = SandboxFileBackend(client=run_client)
        backend: BackendProtocol = DAIVCompositeBackend(
            default=sandbox_backend, routes={}, artifacts_root=WORKSPACE_PATH
        )
    else:
        # Disk-backed: a composite maps the same /workspace namespace onto the local clone
        # (/workspace/repo), the shared skills cache (/workspace/skills) and a per-run scratch dir
        # (/workspace/tmp + offloaded artifacts). The fence keeps the agent's file tools inside
        # those three subtrees; bash is sandbox-only so it needs no equivalent here.
        backend = build_disk_workspace_backend(Path(ctx.gitrepo.working_dir))
        main_agent_permissions = WORKSPACE_FENCE_PERMISSIONS

    # The run's absolute repo root, shared with subagents so their filesystem path directives name
    # the same root the main agent's prompt does (``dynamic_daiv_system_prompt`` derives the same value).
    working_directory = f"{agent_root}/"

    # Fetched before subagents are built so the general-purpose and custom subagents inherit the
    # parent's MCP toolset — otherwise a `task` delegation that calls an MCP tool fails with
    # "command not found". Explore and the code-review detectors stay deliberately scoped and don't
    # receive it.
    mcp_tools = await MCPToolkit.get_tools()

    subagents = [
        create_general_purpose_subagent(
            model,
            backend,
            ctx,
            working_directory,
            sandbox_enabled=_sandbox_enabled,
            web_search_enabled=_web_search_enabled,
            web_fetch_enabled=_web_fetch_enabled,
            fallback_models=fallback_models,
            client=run_client,
            sandbox_backend=sandbox_backend,
            mcp_tools=mcp_tools,
        ),
        create_explore_subagent(backend, working_directory, sandbox_enabled=_sandbox_enabled),
        *load_builtin_code_review_detectors(
            model,
            backend,
            ctx,
            working_directory,
            sandbox_enabled=_sandbox_enabled,
            fallback_models=fallback_models,
            client=run_client,
            sandbox_backend=sandbox_backend,
        ),
    ]

    custom_subagents = await load_custom_subagents(
        model=model,
        backend=backend,
        runtime=ctx,
        sources=[f"{agent_root}/{source}" for source in SUBAGENTS_SOURCES],
        working_directory=working_directory,
        sandbox_enabled=_sandbox_enabled,
        web_search_enabled=_web_search_enabled,
        web_fetch_enabled=_web_fetch_enabled,
        fallback_models=fallback_models,
        client=run_client,
        sandbox_backend=sandbox_backend,
        mcp_tools=mcp_tools,
    )
    subagents.extend(custom_subagents)

    user_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        # DAIVTodoListMiddleware (a subclass), not the bare TodoListMiddleware: the harness profile
        # excludes the base by exact type, so a bare instance here would be dropped alongside the one
        # create_deep_agent auto-adds, leaving the main agent with no write_todos. See todos.py.
        DAIVTodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=_sandbox_enabled)),
        *([SlashCommandMiddleware(subagents=subagents)] if ctx.config.slash_commands.enabled else []),
        *(
            [SandboxMiddleware(agent_root=agent_root, client=run_client, sandbox_backend=sandbox_backend)]
            if _sandbox_enabled
            else []
        ),
        SkillsMiddleware(
            backend=backend,
            sources=[(global_skills_source, "Global"), *[f"{agent_root}/{source}" for source in SKILLS_SOURCES]],
            sandbox_enabled=_sandbox_enabled,
        ),
        *([WebSearchMiddleware()] if _web_search_enabled else []),
        *([WebFetchMiddleware()] if _web_fetch_enabled else []),
        *([ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:])] if fallback_models else []),
        # Web search/fetch, git-platform, and MCP tools are all deferred behind tool_search; only the
        # file/bash/todo core in ALWAYS_LOADED_TOOLS is eagerly bound.
        *deferred_tools_middleware(ALWAYS_LOADED_TOOLS, mcp_tools),
        # Before the caching middleware so the cache-control placement sees the final
        # message list, including any injected budget reminder.
        # finalize (not raise) on the parent: a raise would skip after_agent (publish/patch
        # capture/sandbox teardown) and discard work — the failure mode StepBudget guards against.
        LoopBreakerMiddleware(terminal="finalize"),
        StepBudgetMiddleware(),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        ensure_non_empty_response,
        # Must stay after SandboxMiddleware: after_agent hooks run in REVERSE registration order,
        # so the turn-end publish/patch-capture runs while the sandbox session is still alive
        # (SandboxMiddleware.aafter_agent closes it).
        GitMiddleware(
            auto_commit_changes=auto_commit_changes, capture_patch=capture_patch, sandbox_backend=sandbox_backend
        ),
        GitPlatformMiddleware(git_platform=ctx.git_platform, backend=backend),
        dynamic_daiv_system_prompt,
        RepositoryMemoryMiddleware(),
        *(middleware or []),
    ]

    initial_tools = direct_mcp_tools(mcp_tools)

    deep_agent = create_deep_agent(
        model=model,
        tools=initial_tools,
        system_prompt=None,
        middleware=user_middleware,
        subagents=subagents,
        memory=[f"{agent_root}/{ctx.config.context_file_name}", f"{agent_root}/{AGENTS_MEMORY_PATH}"],
        backend=backend,
        permissions=main_agent_permissions,
        interrupt_on=interrupt_on,
        context_schema=RuntimeCtx,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name="DAIV Agent",
    )
    # recursion_limit counts graph supersteps, not model turns. With every per-turn
    # middleware implemented via wrap_model_call (zero extra nodes), one model+tools cycle
    # costs 2 supersteps, so the default 500 ≈ 250 tool-call turns. Registering a
    # before_model/after_model hook adds a node to EVERY cycle and silently shrinks that
    # budget (3 steps/turn ≈ 165 turns) — keep per-turn hooks out of the stack.
    return deep_agent.with_config({"recursion_limit": site_settings.agent_recursion_limit})
