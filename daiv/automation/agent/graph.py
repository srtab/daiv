import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from django.utils import timezone

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
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
    SKILLS_SOURCES,
    SUBAGENTS_SOURCES,
    ModelName,
)
from automation.agent.deferred.conf import settings as deferred_settings
from automation.agent.mcp.toolkits import MCPToolkit
from automation.agent.middlewares.deferred_tools import DeferredToolsMiddleware
from automation.agent.middlewares.ensure_response import ensure_non_empty_response
from automation.agent.middlewares.file_system import FILESYSTEM_ABSOLUTE_PATH_DIRECTIVE
from automation.agent.middlewares.git import GitMiddleware
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.logging import ToolCallLoggingMiddleware
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from automation.agent.middlewares.skills import SkillsMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.prompts import DAIV_SYSTEM_PROMPT, REPO_RELATIVE_SYSTEM_REMINDER, WRITE_TODOS_SYSTEM_PROMPT
from automation.agent.subagents import create_explore_subagent, create_general_purpose_subagent, load_custom_subagents
from codebase.base import GitPlatform
from codebase.context import RuntimeCtx
from codebase.utils import get_repo_ref
from core.constants import BOT_NAME
from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

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


OUTPUT_INVARIANTS_SYSTEM_PROMPT = f"""\
<output_invariants>
Applies to ALL user-visible text:

- NEVER include "/repo/" anywhere in user-visible output.
- Any repository file path shown to the user MUST be repo-relative (no leading "/").
  <example>/repo/daiv/core/utils.py -> daiv/core/utils.py</example>
- Code reference labels MUST be repo-relative paths (e.g. `daiv/core/utils.py:42`), but hrefs should use platform-native blob URLs with branch refs.
- Before emitting any user-visible text, check for "/repo/" and rewrite to repo-relative form.

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
    agent_path = Path(context.gitrepo.working_dir)

    daiv_system_prompt = await DAIV_SYSTEM_PROMPT.aformat(
        current_date=timezone.now().strftime("%d %B, %Y"),
        bot_name=BOT_NAME,
        bot_username=context.bot_username,
        repository_url=context.repository.html_url,
        gitlab_platform=context.git_platform == GitPlatform.GITLAB,
        github_platform=context.git_platform == GitPlatform.GITHUB,
        bash_tool_enabled=BASH_TOOL_NAME in [tool.name for tool in request.tools],
        working_directory=f"/{agent_path.name}/",
        current_branch=get_repo_ref(context.gitrepo),
    )

    # The harness profile sets ``base_system_prompt=""`` to suppress upstream's
    # BASE_AGENT_PROMPT, but model-level profiles (e.g. anthropic:claude-opus-4-7)
    # still contribute a ``system_prompt_suffix`` we want to keep. Strip to drop
    # leading whitespace introduced by an empty base + suffix concat.
    inherited = (request.system_prompt or "").strip()
    inherited_system_prompt = f"{inherited}\n\n" if inherited else ""

    return (
        OUTPUT_INVARIANTS_SYSTEM_PROMPT
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
    fallback_models = [
        BaseAgent.get_model(model=model_name, thinking_level=thinking_level) for model_name in model_names[1:]
    ]

    _sandbox_enabled = sandbox_enabled if sandbox_enabled is not None else ctx.config.sandbox.enabled
    _web_fetch_enabled = web_fetch_enabled if web_fetch_enabled is not None else site_settings.web_fetch_enabled
    _web_search_enabled = web_search_enabled if web_search_enabled is not None else site_settings.web_search_enabled

    agent_path = Path(ctx.gitrepo.working_dir)
    backend = FilesystemBackend(root_dir=agent_path.parent, virtual_mode=True)

    subagents = [
        create_general_purpose_subagent(
            model,
            backend,
            ctx,
            sandbox_enabled=_sandbox_enabled,
            web_search_enabled=_web_search_enabled,
            web_fetch_enabled=_web_fetch_enabled,
            fallback_models=fallback_models,
        ),
        create_explore_subagent(backend),
    ]

    custom_subagents = await load_custom_subagents(
        model=model,
        backend=backend,
        runtime=ctx,
        sources=[f"/{agent_path.name}/{source}" for source in SUBAGENTS_SOURCES],
        sandbox_enabled=_sandbox_enabled,
        web_search_enabled=_web_search_enabled,
        web_fetch_enabled=_web_fetch_enabled,
        fallback_models=fallback_models,
    )
    subagents.extend(custom_subagents)

    mcp_tools = await MCPToolkit.get_tools()

    user_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=_sandbox_enabled)),
        SkillsMiddleware(
            backend=backend,
            sources=[GLOBAL_SKILLS_PATH, *[f"/{agent_path.name}/{source}" for source in SKILLS_SOURCES]],
            subagents=subagents,
        ),
        *([SandboxMiddleware(backend=backend, working_dir=agent_path)] if _sandbox_enabled else []),
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
        GitMiddleware(auto_commit_changes=auto_commit_changes),
        GitPlatformMiddleware(git_platform=ctx.git_platform),
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
        memory=[f"/{agent_path.name}/{ctx.config.context_file_name}", f"/{agent_path.name}/{AGENTS_MEMORY_PATH}"],
        backend=backend,
        interrupt_on=interrupt_on,
        context_schema=RuntimeCtx,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name="DAIV Agent",
    )
    return deep_agent.with_config({"recursion_limit": site_settings.agent_recursion_limit})
