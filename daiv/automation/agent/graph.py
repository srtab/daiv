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
    TodoListMiddleware,
    dynamic_prompt,
)

from automation.agent.base import BaseAgent, ThinkingLevel
from automation.agent.constants import (
    AGENTS_MEMORY_PATH,
    GLOBAL_SKILLS_PATH,
    GLOBAL_SKILLS_ROUTE,
    REPO_PATH,
    SKILLS_CACHE_PATH,
    SKILLS_PATH,
    SKILLS_SOURCES,
    SUBAGENTS_SOURCES,
    WORKSPACE_PATH,
    ModelName,
)
from automation.agent.deferred.conf import settings as deferred_settings
from automation.agent.mcp.toolkits import MCPToolkit
from automation.agent.middlewares.deferred_tools import DeferredToolsMiddleware
from automation.agent.middlewares.ensure_response import ensure_non_empty_response
from automation.agent.middlewares.file_system import (
    FILESYSTEM_ABSOLUTE_PATH_DIRECTIVE,
    DAIVCompositeBackend,
    DAIVFilesystemBackend,
    SandboxFileBackend,
)
from automation.agent.middlewares.git import GitMiddleware
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.logging import ToolCallLoggingMiddleware
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from automation.agent.middlewares.skills import SkillsMiddleware
from automation.agent.middlewares.slash_commands import SlashCommandMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.prompts import DAIV_SYSTEM_PROMPT, REPO_RELATIVE_SYSTEM_REMINDER, WRITE_TODOS_SYSTEM_PROMPT
from automation.agent.subagents import create_explore_subagent, create_general_purpose_subagent, load_custom_subagents
from codebase.base import GitPlatform
from codebase.context import RuntimeCtx
from codebase.utils import get_repo_ref
from core.constants import BOT_NAME
from core.sandbox.client import get_run_sandbox_client
from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.backends.protocol import BackendProtocol
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


logger = logging.getLogger("daiv.agent")


# Tools always bound to the model; everything else is deferred behind tool_search.
ALWAYS_LOADED_TOOLS = frozenset({
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "write_todos",
    "skill",
    "task",
})


class _Unset:
    """Sentinel to distinguish 'not provided' from ``None`` in function defaults."""


def _output_invariants_system_prompt(working_directory: str) -> str:
    """Output invariants keyed to the run's absolute repo prefix (``/workspace/repo/`` for
    sandbox runs, ``/<clone-name>/`` for disk-backed runs)."""
    prefix = working_directory.rstrip("/") + "/"
    return f"""\
<output_invariants>
Applies to ALL user-visible text:

- NEVER include "{prefix}" anywhere in user-visible output.
- Any repository file path shown to the user MUST be repo-relative (no leading "/").
  <example>{prefix}daiv/core/utils.py -> daiv/core/utils.py</example>
- Code reference labels MUST be repo-relative paths (e.g. `daiv/core/utils.py:42`), but hrefs should use platform-native blob URLs with branch refs.
- Before emitting any user-visible text, check for "{prefix}" and rewrite to repo-relative form.

{FILESYSTEM_ABSOLUTE_PATH_DIRECTIVE}
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
    # Sandbox-enabled runs operate under the sandbox-authoritative /workspace/repo; disk-backed
    # runs see the local clone's basename. Mirror create_daiv_agent's agent_root decision so the
    # prompt's working directory and output invariants match the paths the agent's tools use.
    if context.sandbox and context.sandbox.enabled:
        working_directory = f"{REPO_PATH}/"
    else:
        working_directory = f"/{Path(context.gitrepo.working_dir).name}/"

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

    sandbox_backend: SandboxFileBackend | None = None
    run_client = None
    if _sandbox_enabled:
        # Sandbox-authoritative: one backend serves all of /workspace (repo at
        # /workspace/repo, seeded skills at /workspace/skills, scratchpad at
        # /workspace/tmp). The agent uses these sandbox-absolute paths directly, so the
        # backend is a pass-through, bound to the run's session by
        # SandboxMiddleware.abefore_agent once the session exists.
        agent_root = REPO_PATH
        global_skills_source = SKILLS_PATH
        # Read the run-scoped sandbox client opened by set_runtime_ctx EXACTLY ONCE, here, and inject
        # it explicitly everywhere it's needed. The client is supplied to SandboxFileBackend at
        # construction, so the FilesystemMiddleware/SummarizationMiddleware artifacts_root machinery
        # (which needs a CompositeBackend) still works, and the session is bound late by
        # SandboxMiddleware via bind_session — no composite-unwrapping required.
        #
        # Wrap the single /workspace backend in a composite purely to carry an
        # ``artifacts_root`` under /workspace. The middlewares that offload to the
        # filesystem (FilesystemMiddleware tool-result + HumanMessage eviction,
        # SummarizationMiddleware history) derive their write prefix from
        # ``artifacts_root`` *only when the backend is a CompositeBackend*, defaulting
        # to "/" otherwise. A bare SandboxFileBackend would send those writes to
        # ``/large_tool_results`` / ``/conversation_history`` at the container root —
        # outside /workspace, which the sandbox rejects — so eviction would silently
        # no-op. There is no route: every /workspace path falls through to the default
        # backend with its full path, identity-mapped into the sandbox.
        run_client = get_run_sandbox_client()
        sandbox_backend = SandboxFileBackend(client=run_client)
        backend: BackendProtocol = DAIVCompositeBackend(
            default=sandbox_backend, routes={}, artifacts_root=WORKSPACE_PATH
        )
    else:
        # Sandbox-disabled runs keep the disk-backed composite: repo files from disk;
        # ``/skills/`` from the shared SKILLS_CACHE_PATH so per-turn skill uploads become a
        # no-op once primed. Preserves repoless/no-sandbox flows.
        agent_path = Path(ctx.gitrepo.working_dir)
        agent_root = f"/{agent_path.name}"
        global_skills_source = GLOBAL_SKILLS_PATH
        repo_backend = DAIVFilesystemBackend(root_dir=agent_path.parent, virtual_mode=True)
        skills_backend = DAIVFilesystemBackend(root_dir=SKILLS_CACHE_PATH, virtual_mode=True)
        backend = DAIVCompositeBackend(default=repo_backend, routes={GLOBAL_SKILLS_ROUTE: skills_backend})

    subagents = [
        create_general_purpose_subagent(
            model,
            backend,
            ctx,
            sandbox_enabled=_sandbox_enabled,
            web_search_enabled=_web_search_enabled,
            web_fetch_enabled=_web_fetch_enabled,
            fallback_models=fallback_models,
            client=run_client,
        ),
        create_explore_subagent(backend),
    ]

    custom_subagents = await load_custom_subagents(
        model=model,
        backend=backend,
        runtime=ctx,
        sources=[f"{agent_root}/{source}" for source in SUBAGENTS_SOURCES],
        sandbox_enabled=_sandbox_enabled,
        web_search_enabled=_web_search_enabled,
        web_fetch_enabled=_web_fetch_enabled,
        fallback_models=fallback_models,
        client=run_client,
    )
    subagents.extend(custom_subagents)

    mcp_tools = await MCPToolkit.get_tools()

    user_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=_sandbox_enabled)),
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
        *(
            [
                DeferredToolsMiddleware(
                    always_loaded=ALWAYS_LOADED_TOOLS,
                    extra_tools=mcp_tools,
                    top_k_default=deferred_settings.TOP_K_DEFAULT,
                    top_k_max=deferred_settings.TOP_K_MAX,
                )
            ]
            if deferred_settings.ENABLED
            else []
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        ensure_non_empty_response,
        GitMiddleware(auto_commit_changes=auto_commit_changes, sandbox_backend=sandbox_backend),
        GitPlatformMiddleware(git_platform=ctx.git_platform, backend=backend),
        dynamic_daiv_system_prompt,
        *(middleware or []),
    ]

    initial_tools = [] if deferred_settings.ENABLED else mcp_tools

    deep_agent = create_deep_agent(
        model=model,
        tools=initial_tools,
        system_prompt=None,
        middleware=user_middleware,
        subagents=subagents,
        memory=[f"{agent_root}/{ctx.config.context_file_name}", f"{agent_root}/{AGENTS_MEMORY_PATH}"],
        backend=backend,
        interrupt_on=interrupt_on,
        context_schema=RuntimeCtx,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name="DAIV Agent",
    )
    return deep_agent.with_config({"recursion_limit": site_settings.agent_recursion_limit})
