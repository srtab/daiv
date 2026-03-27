from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
    ModelFallbackMiddleware,
    ModelRequest,
    SummarizationMiddleware,
    TodoListMiddleware,
    dynamic_prompt,
)

from automation.agent.base import BaseAgent, ThinkingLevel
from automation.agent.conf import settings
from automation.agent.constants import AGENTS_MEMORY_PATH, SKILLS_SOURCES, SUBAGENTS_SOURCES, ModelName
from automation.agent.mcp.toolkits import MCPToolkit
from automation.agent.middlewares.ensure_response import ensure_non_empty_response
from automation.agent.middlewares.file_system import FilesystemMiddleware
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
from automation.conf import settings as automation_settings
from codebase.base import GitPlatform
from codebase.context import AgentCtx, LocalRuntimeCtx, RuntimeCtx
from codebase.utils import get_repo_ref
from core.constants import BOT_NAME

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


OUTPUT_INVARIANTS_SYSTEM_PROMPT = """\
<output_invariants>
Applies to ALL user-visible text:

- NEVER include "/repo/" anywhere in user-visible output.
- Any repository file path shown to the user MUST be repo-relative (no leading "/").
  <example>/repo/daiv/core/utils.py -> daiv/core/utils.py</example>
- Code reference labels MUST be repo-relative paths (e.g. `daiv/core/utils.py:42`), but hrefs should use platform-native blob URLs with branch refs.
- Before emitting any user-visible text, check for "/repo/" and rewrite to repo-relative form.
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
    context: AgentCtx = request.runtime.context
    is_local = isinstance(context, LocalRuntimeCtx)

    # Build shared template kwargs, then extend with mode-specific ones
    kwargs: dict = {
        "current_date": datetime.now(UTC).strftime("%d %B, %Y"),
        "bot_name": BOT_NAME,
        "bot_username": context.bot_username,
        "bash_tool_enabled": BASH_TOOL_NAME in {tool.name for tool in request.tools},
        "local_mode": is_local,
    }

    if is_local:
        kwargs["working_directory"] = str(context.working_dir)
        kwargs["current_branch"] = get_repo_ref(context.gitrepo) if context.gitrepo else ""
    else:
        context = cast("RuntimeCtx", context)
        agent_path = Path(context.gitrepo.working_dir)
        kwargs["working_directory"] = f"/{agent_path.name}/"
        kwargs["current_branch"] = get_repo_ref(context.gitrepo)
        kwargs["repository_url"] = context.repository.html_url
        kwargs["gitlab_platform"] = context.git_platform == GitPlatform.GITLAB
        kwargs["github_platform"] = context.git_platform == GitPlatform.GITHUB

    daiv_system_prompt = await DAIV_SYSTEM_PROMPT.aformat(**kwargs)

    inherited_system_prompt = ""
    if request.system_prompt:
        inherited_system_prompt = request.system_prompt + "\n\n"

    prompt_parts = [cast("str", daiv_system_prompt.content).strip()]

    if not is_local:
        prompt_parts.insert(0, OUTPUT_INVARIANTS_SYSTEM_PROMPT)
        prompt_parts.append(inherited_system_prompt + REPO_RELATIVE_SYSTEM_REMINDER)
    elif inherited_system_prompt:
        prompt_parts.append(inherited_system_prompt.rstrip())

    return "\n\n".join(prompt_parts)


def dynamic_write_todos_system_prompt(bash_tool_enabled: bool) -> str:
    """
    Dynamic prompt for the write todos system.
    """
    return cast("str", WRITE_TODOS_SYSTEM_PROMPT.format(bash_tool_enabled=bash_tool_enabled).content)


async def create_daiv_agent(
    model_names: Sequence[ModelName | str] = (settings.MODEL_NAME, settings.FALLBACK_MODEL_NAME),
    thinking_level: ThinkingLevel | None = settings.THINKING_LEVEL,
    *,
    ctx: AgentCtx,
    auto_commit_changes: bool = True,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    debug: bool = False,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
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
        ctx: The runtime context (platform or local).
        auto_commit_changes: Whether to commit the changes to the repository when the agent finishes.
        checkpointer: The checkpointer to use for the agent.
        store: The store to use for the agent.
        debug: Whether to enable debug mode for the agent.
        interrupt_on: The interrupt on configuration for the agent.
        sandbox_enabled: Whether to enable the sandbox for the agent. If None, fallback to the config default.
        web_fetch_enabled: Whether to enable web fetch for the agent. If None, fallback to the config default.
        web_search_enabled: Whether to enable web search for the agent. If None, fallback to the config default.

    Returns:
        The DAIV agent.
    """
    is_local = isinstance(ctx, LocalRuntimeCtx)

    model = BaseAgent.get_model(model=model_names[0], thinking_level=thinking_level)
    fallback_models = [
        BaseAgent.get_model(model=model_name, thinking_level=thinking_level) for model_name in model_names[1:]
    ]

    _summarization_defaults = compute_summarization_defaults(model)
    _sandbox_enabled = sandbox_enabled if sandbox_enabled is not None else ctx.config.sandbox.enabled
    _web_fetch_enabled = web_fetch_enabled if web_fetch_enabled is not None else automation_settings.WEB_FETCH_ENABLED
    _web_search_enabled = (
        web_search_enabled if web_search_enabled is not None else automation_settings.WEB_SEARCH_ENABLED
    )

    if is_local:
        agent_path = ctx.working_dir
        backend = FilesystemBackend(root_dir=agent_path, virtual_mode=True)
    else:
        agent_path = Path(ctx.gitrepo.working_dir)
        backend = FilesystemBackend(root_dir=agent_path.parent, virtual_mode=True)

    # Build path prefix for filesystem sources
    # For platform mode: /repo/ (agent_path.name inside the parent root)
    # For local mode: / (the cwd itself is the root)
    path_prefix = f"/{agent_path.name}" if not is_local else ""

    # Create subagents list to be shared between middlewares
    subagents = [
        create_general_purpose_subagent(
            model,
            backend,
            ctx,
            sandbox_enabled=_sandbox_enabled,
            web_search_enabled=_web_search_enabled,
            web_fetch_enabled=_web_fetch_enabled,
        ),
        create_explore_subagent(backend),
    ]

    # Load custom subagents from the repository
    custom_subagents = await load_custom_subagents(
        model=model,
        backend=backend,
        runtime=ctx,
        sources=[f"{path_prefix}/{source}" for source in SUBAGENTS_SOURCES],
        sandbox_enabled=_sandbox_enabled,
        web_search_enabled=_web_search_enabled,
        web_fetch_enabled=_web_fetch_enabled,
    )
    subagents.extend(custom_subagents)

    agent_conditional_middlewares = []

    if _web_search_enabled:
        agent_conditional_middlewares.append(WebSearchMiddleware())
    if _web_fetch_enabled:
        agent_conditional_middlewares.append(WebFetchMiddleware())
    if _sandbox_enabled:
        agent_conditional_middlewares.append(SandboxMiddleware())
    if fallback_models:
        agent_conditional_middlewares.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))
    if interrupt_on is not None:
        agent_conditional_middlewares.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    agent_middleware = [
        TodoListMiddleware(system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=_sandbox_enabled)),
        MemoryMiddleware(
            backend=backend,
            sources=[f"{path_prefix}/{ctx.config.context_file_name}", f"{path_prefix}/{AGENTS_MEMORY_PATH}"],
        ),
        SkillsMiddleware(
            backend=backend, sources=[f"{path_prefix}/{source}" for source in SKILLS_SOURCES], subagents=subagents
        ),
        SubAgentMiddleware(backend=backend, subagents=subagents),
        *agent_conditional_middlewares,
        FilesystemMiddleware(backend=backend),
    ]

    # Git middlewares only apply in platform mode
    if not is_local:
        agent_middleware.append(GitMiddleware(auto_commit_changes=auto_commit_changes))
        agent_middleware.append(GitPlatformMiddleware(git_platform=ctx.git_platform))

    agent_middleware.extend([
        SummarizationMiddleware(
            model=model,
            backend=backend,
            trigger=_summarization_defaults["trigger"],
            keep=_summarization_defaults["keep"],
            trim_tokens_to_summarize=None,
            truncate_args_settings=_summarization_defaults["truncate_args_settings"],
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        ensure_non_empty_response,
        PatchToolCallsMiddleware(),
        dynamic_daiv_system_prompt,
    ])

    return create_agent(
        model,
        tools=await MCPToolkit.get_tools(),
        middleware=agent_middleware,
        context_schema=type(ctx),
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name="DAIV Agent",
    ).with_config({"recursion_limit": settings.RECURSION_LIMIT})
