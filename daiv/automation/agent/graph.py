import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import django
from django.utils import timezone

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.graph import BASE_AGENT_PROMPT
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelFallbackMiddleware,
    ModelRequest,
    SummarizationMiddleware,
    TodoListMiddleware,
    dynamic_prompt,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML

from automation.agent.base import BaseAgent, ThinkingLevel
from automation.agent.conf import settings
from automation.agent.constants import DAIV_MEMORY_PATH, SKILLS_SOURCES
from automation.agent.mcp.toolkits import MCPToolkit
from automation.agent.middlewares.file_system import FilesystemMiddleware
from automation.agent.middlewares.git import GitMiddleware
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.logging import ToolCallLoggingMiddleware
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.middlewares.sandbox import BASH_TOOL_NAME, SandboxMiddleware
from automation.agent.middlewares.skills import SkillsMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.prompts import DAIV_SYSTEM_PROMPT, WRITE_TODOS_SYSTEM_PROMPT
from automation.agent.subagents import (
    create_changelog_subagent,
    create_explore_subagent,
    create_general_purpose_subagent,
)
from automation.conf import settings as automation_settings
from codebase.base import Scope
from codebase.context import RuntimeCtx, set_runtime_ctx
from core.constants import BOT_NAME

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore

    from automation.agent.constants import ModelName


DEFAULT_SUMMARIZATION_TRIGGER = ("tokens", 170000)
DEFAULT_SUMMARIZATION_KEEP = ("messages", 6)


OUTPUT_INVARIANTS_SYSTEM_PROMPT = """\
<output_invariants>
Applies to ALL user-visible text:

- NEVER include "/repo/" anywhere in user-visible output.
- Any repository file path shown to the user MUST be repo-relative (no leading "/").
  <example>/repo/daiv/core/utils.py -> daiv/core/utils.py</example>
- Code references MUST use repo-relative paths: [path:line](path#Lline)
- Pre-send check: if your draft contains "/repo/", rewrite before sending.
</output_invariants>"""


@dynamic_prompt
async def dynamic_daiv_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic prompt for the DAIV system.

    Args:
        request (ModelRequest): The request to the model.

    Returns:
        str: The dynamic prompt for the DAIV system.
    """
    tool_names = [tool.name for tool in request.tools]
    agent_path = Path(request.runtime.context.repo.working_dir)

    system_prompt = await DAIV_SYSTEM_PROMPT.aformat(
        current_date_time=timezone.now().strftime("%d %B, %Y"),
        bot_name=BOT_NAME,
        bot_username=request.runtime.context.bot_username,
        repository=request.runtime.context.repo_id,
        git_platform=request.runtime.context.git_platform.value,
        bash_tool_enabled=BASH_TOOL_NAME in tool_names,
        working_directory=f"/{agent_path.name}/",
    )
    return (
        BASE_AGENT_PROMPT
        + "\n\n"
        + OUTPUT_INVARIANTS_SYSTEM_PROMPT
        + "\n\n"
        + request.system_prompt
        + "\n\n"
        + system_prompt.content.strip()
    )


def dynamic_write_todos_system_prompt(bash_tool_enabled: bool) -> str:
    """
    Dynamic prompt for the write todos system.
    """
    return WRITE_TODOS_SYSTEM_PROMPT.format(bash_tool_enabled=bash_tool_enabled).content


async def create_daiv_agent(
    model_names: list[ModelName | str] = (settings.MODEL_NAME, settings.FALLBACK_MODEL_NAME),
    thinking_level: ThinkingLevel | None = settings.THINKING_LEVEL,
    *,
    ctx: RuntimeCtx,
    auto_commit_changes: bool = True,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    debug: bool = False,
    cache: bool = False,
    offline: bool = False,
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
        cache: Whether to enable cache for the agent.
        offline: Whether to enable offline mode for the agent.

    Returns:
        The DAIV agent.
    """
    agent_path = Path(ctx.repo.working_dir)

    model = BaseAgent.get_model(model=model_names[0], thinking_level=thinking_level)
    fallback_models = [
        BaseAgent.get_model(model=model_name, thinking_level=thinking_level) for model_name in model_names[1:]
    ]

    summarization_trigger = DEFAULT_SUMMARIZATION_TRIGGER
    summarization_keep = DEFAULT_SUMMARIZATION_KEEP

    if isinstance(model.profile, dict) and isinstance(model.profile.get("max_input_tokens"), int):
        summarization_trigger = ("fraction", 0.85)
        summarization_keep = ("fraction", 0.10)

    backend = FilesystemBackend(root_dir=agent_path.parent, virtual_mode=True)

    subagent_default_middlewares = [
        SummarizationMiddleware(
            model=model, trigger=summarization_trigger, keep=summarization_keep, trim_tokens_to_summarize=None
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        PatchToolCallsMiddleware(),
    ]

    if fallback_models:
        subagent_default_middlewares.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))

    agent_conditional_middlewares = []

    if not offline:
        agent_conditional_middlewares.append(WebSearchMiddleware())
    if not offline and automation_settings.WEB_FETCH_ENABLED:
        agent_conditional_middlewares.append(WebFetchMiddleware())
    if ctx.config.sandbox.enabled:
        agent_conditional_middlewares.append(SandboxMiddleware())
    if fallback_models:
        agent_conditional_middlewares.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))

    agent_middleware = [
        TodoListMiddleware(
            system_prompt=dynamic_write_todos_system_prompt(bash_tool_enabled=ctx.config.sandbox.enabled)
        ),
        MemoryMiddleware(
            backend=backend,
            sources=[f"/{agent_path.name}/{ctx.config.context_file_name}", f"/{agent_path.name}/{DAIV_MEMORY_PATH}"],
        ),
        SkillsMiddleware(backend=backend, sources=[f"/{agent_path.name}/{source}" for source in SKILLS_SOURCES]),
        SubAgentMiddleware(
            default_model=model,
            default_middleware=subagent_default_middlewares,
            general_purpose_agent=False,
            subagents=[
                create_general_purpose_subagent(backend, ctx, offline=offline),
                create_explore_subagent(backend, ctx),
                create_changelog_subagent(backend, ctx),
            ],
        ),
        *agent_conditional_middlewares,
        FilesystemMiddleware(backend=backend),
        GitMiddleware(auto_commit_changes=auto_commit_changes),
        GitPlatformMiddleware(),
        SummarizationMiddleware(
            model=model, trigger=summarization_trigger, keep=summarization_keep, trim_tokens_to_summarize=None
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        PatchToolCallsMiddleware(),
        dynamic_daiv_system_prompt,
    ]

    return create_agent(
        model,
        tools=await MCPToolkit.get_tools(),
        middleware=agent_middleware,
        context_schema=RuntimeCtx,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name="DAIV Agent",
        cache=cache,
    ).with_config({"recursion_limit": settings.RECURSION_LIMIT})


async def main():
    session = PromptSession(
        message=HTML('<style fg="#ffffff">></style> '),
        complete_while_typing=True,  # Show completions as you type
        complete_in_thread=True,  # Async completion prevents menu freezing
        mouse_support=False,
        enable_open_in_editor=True,  # Allow Ctrl+X Ctrl+E to open external editor
        enable_history_search=True,
        wrap_lines=True,
        reserve_space_for_menu=7,  # Reserve space for completion menu to show 5-6 results
    )
    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        agent = await create_daiv_agent(
            ctx=ctx, model_names=["openrouter:z-ai/glm-4.7"], store=InMemoryStore(), checkpointer=InMemorySaver()
        )
        while True:
            user_input = await session.prompt_async()
            async for message_chunk, _metadata in agent.astream(
                {"messages": [{"role": "user", "content": user_input}]},
                context=ctx,
                config={"configurable": {"thread_id": "1"}},
                stream_mode="messages",
            ):
                if message_chunk and message_chunk.content and message_chunk.type != "tool":
                    print(message_chunk.content, end="", flush=True)  # noqa: T201
            print()  # noqa: T201


if __name__ == "__main__":
    django.setup()
    asyncio.run(main())
