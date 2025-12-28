import asyncio
from typing import TYPE_CHECKING

from django.conf import django
from django.utils import timezone

from deepagents.graph import BASE_AGENT_PROMPT, SubAgent
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import (
    DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
    DEFAULT_SUBAGENT_PROMPT,
    SubAgentMiddleware,
)
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
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.formatted_text import HTML

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.constants import ModelName
from automation.agents.deepagent.backends import CompositeBackend, FilesystemBackend, StateBackend
from automation.agents.deepagent.conf import settings
from automation.agents.deepagent.middlewares import FilesystemMiddleware
from automation.agents.deepagent.prompts import (
    WRITE_TODOS_SYSTEM_PROMPT,
    daiv_system_prompt,
    explore_system_prompt,
    pipeline_debugger_system_prompt,
)
from automation.agents.middlewares.logging import ToolCallLoggingMiddleware
from automation.agents.middlewares.memory import LongTermMemoryMiddleware
from automation.agents.middlewares.merge_request import job_logs_tool, pipeline_tool
from automation.agents.middlewares.multimodal import InjectImagesMiddleware
from automation.agents.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agents.middlewares.sandbox import SandboxMiddleware
from automation.agents.middlewares.toolkits import MCPToolkit
from automation.agents.middlewares.web_search import WebSearchMiddleware
from automation.agents.skills.middleware import SkillsMiddleware
from codebase.context import RuntimeCtx, set_runtime_ctx
from core.constants import BOT_NAME

if TYPE_CHECKING:
    from langchain.tools import ToolRuntime
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


DEFAULT_SUMMARIZATION_TRIGGER = ("tokens", 170000)
DEFAULT_SUMMARIZATION_KEEP = ("messages", 6)

EXPLORE_SUBAGENT_DESCRIPTION = """Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive analysis across multiple locations and naming conventions."""  # noqa: E501

PIPELINE_DEBUGGER_SUBAGENT_DESCRIPTION = """Specialized agent for investigating the latest CI pipeline/workflow for a merge/pull request. Use this when you need to investigate a pipeline failure and produce a concise RCA, or when the user explicitly requests pipeline investigation, debugging, or status checking."""  # noqa: E501


@dynamic_prompt
def dinamic_daiv_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic prompt for the DAIV system.

    Args:
        request (ModelRequest): The request to the model.

    Returns:
        str: The dynamic prompt for the DAIV system.
    """
    return (
        request.system_prompt
        + "\n\n"
        + daiv_system_prompt.format(
            current_date_time=timezone.now().strftime("%d %B, %Y"),
            bot_name=BOT_NAME,
            bot_username=request.runtime.context.bot_username,
            repository=request.runtime.context.repo_id,
        )
    )


def create_general_purpose_subagent(runtime: RuntimeCtx) -> SubAgent:
    """
    Create the general purpose subagent for the DAIV agent.
    """
    middleware = [WebSearchMiddleware()]

    if runtime.config.sandbox.enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="general-purpose",
        description=DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
        system_prompt=DEFAULT_SUBAGENT_PROMPT,
        middleware=middleware,
    )


def create_explore_subagent(runtime: RuntimeCtx) -> SubAgent:
    """
    Create the explore subagent.
    """
    middleware = []

    if runtime.config.sandbox.enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="explore",
        description=EXPLORE_SUBAGENT_DESCRIPTION,
        system_prompt=explore_system_prompt,
        middleware=middleware,
    )


def create_pipeline_debugger_subagent(runtime: RuntimeCtx) -> SubAgent:
    """
    Create the pipeline debugger subagent.
    """
    middleware = []

    if runtime.config.sandbox.enabled:
        middleware.append(SandboxMiddleware(close_session=False))

    return SubAgent(
        name="pipeline-debugger",
        description=PIPELINE_DEBUGGER_SUBAGENT_DESCRIPTION,
        system_prompt=pipeline_debugger_system_prompt,
        tools=[pipeline_tool, job_logs_tool],
        middleware=middleware,
    )


async def create_daiv_agent(
    model_names: list[ModelName | str] = (settings.MODEL_NAME, settings.FALLBACK_MODEL_NAME),
    thinking_level: ThinkingLevel | None = settings.THINKING_LEVEL,
    *,
    ctx: RuntimeCtx,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    debug: bool = False,
    cache: bool = False,
):
    """
    Create the DAIV agent.

    Args:
        model_names: The model names to use for the agent.
        thinking_level: The thinking level to use for the agent.
        ctx: The runtime context.
        checkpointer: The checkpointer to use for the agent.
        store: The store to use for the agent.
        debug: Whether to enable debug mode for the agent.
        cache: Whether to enable cache for the agent.

    Returns:
        The DAIV agent.
    """
    model = BaseAgent.get_model(model=model_names[0], thinking_level=thinking_level)
    fallback_models = [
        BaseAgent.get_model(model=model_name, thinking_level=thinking_level) for model_name in model_names[1:]
    ]

    summarization_trigger = DEFAULT_SUMMARIZATION_TRIGGER
    summarization_keep = DEFAULT_SUMMARIZATION_KEEP
    supported_image_inputs = False

    if model.profile is not None and isinstance(model.profile, dict):
        if "max_input_tokens" in model.profile and isinstance(model.profile["max_input_tokens"], int):
            summarization_trigger = ("fraction", 0.85)
            summarization_keep = ("fraction", 0.10)
        if "image_inputs" in model.profile and model.profile["image_inputs"] is True:
            supported_image_inputs = True

    def backend(runtime: ToolRuntime):
        return CompositeBackend(
            default=FilesystemBackend(root_dir=ctx.repo.working_dir, virtual_mode=True),
            routes={"/skills/": StateBackend(runtime=runtime)},
        )

    subagents = [create_general_purpose_subagent(ctx), create_explore_subagent(ctx)]
    subagent_middlewares = [
        TodoListMiddleware(system_prompt=WRITE_TODOS_SYSTEM_PROMPT),
        FilesystemMiddleware(backend=backend),
        SummarizationMiddleware(
            model=model, trigger=summarization_trigger, keep=summarization_keep, trim_tokens_to_summarize=None
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        PatchToolCallsMiddleware(),
    ]

    if ctx.scope == "merge_request":
        subagents.append(create_pipeline_debugger_subagent(ctx))

    if fallback_models:
        subagent_middlewares.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))

    deepagent_middleware = [
        TodoListMiddleware(system_prompt=WRITE_TODOS_SYSTEM_PROMPT),
        WebSearchMiddleware(),
        FilesystemMiddleware(backend=backend),
        LongTermMemoryMiddleware(backend=backend),
        SkillsMiddleware(scope=ctx.scope, backend=backend),
        SubAgentMiddleware(default_model=model, default_middleware=subagent_middlewares, subagents=subagents),
        SummarizationMiddleware(
            model=model, trigger=summarization_trigger, keep=summarization_keep, trim_tokens_to_summarize=None
        ),
        AnthropicPromptCachingMiddleware(),
        ToolCallLoggingMiddleware(),
        PatchToolCallsMiddleware(),
        dinamic_daiv_system_prompt,
    ]

    if supported_image_inputs:
        deepagent_middleware.append(InjectImagesMiddleware())

    if ctx.config.sandbox.enabled:
        deepagent_middleware.append(SandboxMiddleware())

    if fallback_models:
        deepagent_middleware.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))

    return create_agent(
        model,
        tools=await MCPToolkit.get_tools(),
        system_prompt=BASE_AGENT_PROMPT,
        middleware=deepagent_middleware,
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
        editing_mode=EditingMode.VI,
        complete_while_typing=True,  # Show completions as you type
        complete_in_thread=True,  # Async completion prevents menu freezing
        mouse_support=False,
        enable_open_in_editor=True,  # Allow Ctrl+X Ctrl+E to open external editor
        reserve_space_for_menu=7,  # Reserve space for completion menu to show 5-6 results
    )
    async with set_runtime_ctx(repo_id="srtab/daiv", ref="main") as ctx:
        agent = await create_daiv_agent(
            ctx=ctx, model_names=[ModelName.CLAUDE_SONNET_4_5], store=InMemoryStore(), checkpointer=InMemorySaver()
        )
        while True:
            user_input = await session.prompt_async()
            async for message_chunk, _metadata in agent.astream(
                {"messages": [{"role": "user", "content": user_input}]},
                context=ctx,
                config={"configurable": {"thread_id": "1"}},
                stream_mode="messages",
            ):
                if message_chunk and message_chunk.content:
                    print(message_chunk.content, end="", flush=True)  # noqa: T201
            print()  # noqa: T201


if __name__ == "__main__":
    django.setup()
    asyncio.run(main())
