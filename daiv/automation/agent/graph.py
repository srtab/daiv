import logging
from typing import TYPE_CHECKING, Any, cast

from django.utils import timezone

from deepagents import create_deep_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    InterruptOnConfig,
    ModelFallbackMiddleware,
    ModelRequest,
    TodoListMiddleware,
    dynamic_prompt,
)

from automation.agent.base import BaseAgent
from automation.agent.constants import REPO_PATH, SKILLS_PATH, SKILLS_SOURCES, SKILLS_TOOL_NAME, SUBAGENTS_SOURCES
from automation.agent.mcp.toolkits import MCPToolkit
from automation.agent.middlewares.artifacts import ArtifactsMiddleware
from automation.agent.middlewares.ask_user_question import AskUserQuestionMiddleware
from automation.agent.middlewares.context_usage import ContextUsageMiddleware
from automation.agent.middlewares.deferred_tools import (
    deferred_tools_middleware,
    direct_mcp_tools,
    inline_tool_definitions_middleware,
)
from automation.agent.middlewares.ensure_response import ensure_non_empty_response
from automation.agent.middlewares.file_system import (
    CUSTOM_TOOL_DESCRIPTIONS,
    WORKSPACE_FS_TOOLS,
    DAIVFilesystemMiddleware,
    filesystem_absolute_path_directive,
)
from automation.agent.middlewares.git import GitMiddleware
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.logging import ToolCallLoggingMiddleware
from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
from automation.agent.middlewares.memory import RepositoryMemoryMiddleware, build_agents_memory_middleware
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from automation.agent.middlewares.skills import SkillsMiddleware
from automation.agent.middlewares.slash_commands import SlashCommandMiddleware
from automation.agent.middlewares.step_budget import StepBudgetMiddleware
from automation.agent.middlewares.summarization import build_summarization_middleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.profile import register as _register_harness_profile
from automation.agent.prompts import AGENTS_MEMORY_SYSTEM_PROMPT, DAIV_SYSTEM_PROMPT, WRITE_TODOS_SYSTEM_PROMPT
from automation.agent.questions import ASK_USER_QUESTION_TOOL_NAME
from automation.agent.subagents import (
    create_explore_subagent,
    create_general_purpose_subagent,
    load_builtin_code_review_detectors,
    load_custom_subagents,
)
from codebase.base import GitPlatform
from codebase.context import RuntimeCtx
from core.constants import BOT_NAME

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore

    from automation.agent.agent_settings import AgentSettings
    from automation.agent.artifacts import ArtifactStore
    from automation.agent.workspace.base import Workspace


logger = logging.getLogger("daiv.agent")

_register_harness_profile()


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
    ASK_USER_QUESTION_TOOL_NAME,
})


def _output_invariants_system_prompt(working_directory: str) -> str:
    """Output invariants keyed to the run's absolute repo prefix (always ``/workspace/repo/`` —
    unified across sandbox and disk-backed runs)."""
    prefix = working_directory.rstrip("/") + "/"
    return f"""\
<output_invariants>
- Show repository paths repo-relative in user-visible text (e.g. `daiv/core/utils.py:42`), never under "{prefix}"; link them with platform-native blob URLs on the branch.

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
        current_branch=context.repo.current_ref,
    )

    # The harness profile sets ``base_system_prompt=""`` to suppress upstream's
    # BASE_AGENT_PROMPT, but model-level profiles (e.g. anthropic:claude-opus-4-7)
    # still contribute a ``system_prompt_suffix`` we want to keep. Strip to drop
    # leading whitespace introduced by an empty base + suffix concat.
    inherited = (request.system_prompt or "").strip()

    return "\n\n".join(
        filter(
            None,
            (
                _output_invariants_system_prompt(working_directory),
                cast("str", daiv_system_prompt.content).strip(),
                inherited,
            ),
        )
    )


def dynamic_write_todos_system_prompt(bash_tool_enabled: bool) -> str:
    """
    Dynamic prompt for the write todos system.
    """
    return cast("str", WRITE_TODOS_SYSTEM_PROMPT.format(bash_tool_enabled=bash_tool_enabled).content)


async def create_daiv_agent(
    *,
    settings: AgentSettings,
    ctx: RuntimeCtx,
    workspace: Workspace,
    artifact_store: ArtifactStore | None = None,
    auto_commit_changes: bool = True,
    capture_patch: bool = False,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    debug: bool = False,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    middleware: list[AgentMiddleware] | None = None,
    ask_user_enabled: bool = True,
):
    """
    Create the DAIV agent.

    Args:
        settings: The run's resolved agent settings: its model chains, recursion limit, web toggles and switches.
        ctx: The runtime context.
        workspace: Where the agent works, built by the run executor: the worker's clone, or the run's sandbox session.
        artifact_store: Where ``publish_artifact`` keeps files and ``fetch_artifact`` reads them; ``None`` leaves both
            tools out (the run executor always passes one).
        auto_commit_changes: Whether to commit the changes to the repository when the agent finishes.
        capture_patch: Whether to expose the run's working-tree diff as ``model_patch`` in the
            output state at turn end. For eval harnesses; keep ``False`` for normal runs.
        checkpointer: The checkpointer to use for the agent.
        store: The store to use for the agent.
        debug: Whether to enable debug mode for the agent.
        interrupt_on: The interrupt on configuration for the agent.
        middleware: The middleware to use for the agent.
        ask_user_enabled: Whether the agent may stop to ask the user; off when nobody can answer during the run.

    Returns:
        The DAIV agent.
    """
    chain = settings.agent
    model = BaseAgent.get_model(model=chain.names[0], thinking_level=chain.thinking_level)
    fallback_models = [
        BaseAgent.get_model(model=model_name, thinking_level=chain.fallback_thinking_level)
        for model_name in chain.names[1:]
    ]

    bash_tool_enabled = workspace.bash is not None

    agent_root = REPO_PATH
    global_skills_source = SKILLS_PATH
    backend = workspace.backend

    # The run's absolute repo root, shared with subagents so their filesystem path directives name
    # the same root the main agent's prompt does (``dynamic_daiv_system_prompt`` derives the same value).
    working_directory = f"{agent_root}/"
    agents_memory = build_agents_memory_middleware(
        backend, agent_root, ctx.config.context_file_name, AGENTS_MEMORY_SYSTEM_PROMPT
    )

    # Fetched before subagents are built so the general-purpose and custom subagents inherit the
    # parent's MCP toolset — otherwise a `task` delegation that calls an MCP tool fails with
    # "command not found". Explore and the code-review detectors stay deliberately scoped and don't
    # receive it.
    mcp_tools = await MCPToolkit.get_tools(user_id=ctx.acting_user_id, overrides=ctx.mcp_overrides)

    subagents = [
        create_general_purpose_subagent(
            model,
            workspace,
            ctx,
            working_directory,
            web_search_enabled=settings.web_search_enabled,
            web_fetch_enabled=settings.web_fetch_enabled,
            cross_project_enabled=settings.cross_project_enabled,
            fallback_models=fallback_models,
            mcp_tools=mcp_tools,
        ),
        create_explore_subagent(workspace, working_directory, models=settings.explore),
        *load_builtin_code_review_detectors(model, backend, working_directory, fallback_models=fallback_models),
    ]

    custom_subagents = await load_custom_subagents(
        model=model,
        workspace=workspace,
        runtime=ctx,
        sources=[f"{agent_root}/{source}" for source in SUBAGENTS_SOURCES],
        working_directory=working_directory,
        web_search_enabled=settings.web_search_enabled,
        web_fetch_enabled=settings.web_fetch_enabled,
        cross_project_enabled=settings.cross_project_enabled,
        fallback_models=fallback_models,
        mcp_tools=mcp_tools,
    )
    subagents.extend(custom_subagents)

    deferred_tools = deferred_tools_middleware(ALWAYS_LOADED_TOOLS, mcp_tools)

    user_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        # Replaces the FilesystemMiddleware create_deep_agent would auto-add: 0.7 merges custom
        # middleware into the base stack by ``.name``, taking the same slot and preserving order.
        # Passed only to restrict the toolset (see WORKSPACE_FS_TOOLS); ``_permissions`` must keep
        # mirroring the ``permissions=`` argument below, which still drives the HITL interrupt rules.
        DAIVFilesystemMiddleware(
            backend=backend,
            custom_tool_descriptions=CUSTOM_TOOL_DESCRIPTIONS,
            tools=WORKSPACE_FS_TOOLS,
            _permissions=workspace.fs_permissions,
        ),
        # Like the filesystem middleware above, these two take the slots of deepagents' same-named defaults.
        build_summarization_middleware(model, backend),
        agents_memory,
        # deepagents 0.7 no longer auto-adds TodoListMiddleware, so DAIV's instance is the only
        # source of write_todos and the harness profile excludes nothing here.
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=bash_tool_enabled)),
        *([SlashCommandMiddleware(subagents=subagents)] if settings.features.slash_commands else []),
        *([SandboxMiddleware(agent_root=agent_root, workspace=workspace)] if bash_tool_enabled else []),
        SkillsMiddleware(
            backend=backend,
            sources=[(global_skills_source, "Global"), *[f"{agent_root}/{source}" for source in SKILLS_SOURCES]],
            copy_global_skills=not workspace.provisions_skills,
        ),
        *([WebSearchMiddleware()] if settings.web_search_enabled else []),
        *([WebFetchMiddleware()] if settings.web_fetch_enabled else []),
        *([ArtifactsMiddleware(workspace=workspace, store=artifact_store)] if artifact_store is not None else []),
        *([ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:])] if fallback_models else []),
        # Web search/fetch, git-platform, and MCP tools are all deferred behind tool_search; only the
        # file/bash/todo core in ALWAYS_LOADED_TOOLS is eagerly bound.
        *deferred_tools,
        *([AskUserQuestionMiddleware()] if ask_user_enabled else []),
        # Before the caching middleware so the cache-control placement sees the final
        # message list, including any injected budget reminder.
        # finalize (not raise) on the parent: a raise would skip after_agent (publish/patch capture)
        # and discard work — the failure mode StepBudget guards against.
        LoopBreakerMiddleware(terminal="finalize"),
        StepBudgetMiddleware(),
        ContextUsageMiddleware(),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        ensure_non_empty_response,
        GitMiddleware(
            workspace=workspace, settings=settings, auto_commit_changes=auto_commit_changes, capture_patch=capture_patch
        ),
        GitPlatformMiddleware(
            git_platform=ctx.git_platform, backend=backend, cross_project_enabled=settings.cross_project_enabled
        ),
        dynamic_daiv_system_prompt,
        RepositoryMemoryMiddleware(enabled=settings.features.memory),
        *(middleware or []),
        *inline_tool_definitions_middleware(deferred_tools),
    ]

    initial_tools = direct_mcp_tools(mcp_tools)

    deep_agent = create_deep_agent(
        model=model,
        tools=initial_tools,
        system_prompt=None,
        middleware=user_middleware,
        subagents=subagents,
        # Still needed: it makes deepagents build the memory slot ``agents_memory`` replaces. Without it the
        # instance would land ahead of the prompt-caching tail instead of after it.
        memory=agents_memory.sources,
        backend=backend,
        permissions=workspace.fs_permissions,
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
    return deep_agent.with_config({"recursion_limit": settings.recursion_limit})
