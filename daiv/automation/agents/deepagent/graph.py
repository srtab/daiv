import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import django
from django.utils import timezone

from deepagents.graph import BASE_AGENT_PROMPT, SubAgent
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

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.constants import ModelName
from automation.agents.deepagent.backends import FilesystemBackend
from automation.agents.deepagent.conf import settings
from automation.agents.deepagent.middlewares import FilesystemMiddleware
from automation.agents.deepagent.prompts import (
    daiv_system_prompt,
    explore_system_prompt,
    pipeline_debugger_system_prompt,
)
from automation.agents.middleware import (
    AnthropicPromptCachingMiddleware,
    InjectImagesMiddleware,
    LongTermMemoryMiddleware,
)
from automation.agents.skills.middleware import SkillsMiddleware
from automation.agents.tools.merge_request import MergeRequestMiddleware, job_logs_tool, pipeline_tool
from automation.agents.tools.sandbox import SandboxMiddleware
from automation.agents.tools.toolkits import MCPToolkit
from automation.agents.tools.web_search import WebSearchMiddleware
from codebase.context import RuntimeCtx, set_runtime_ctx
from core.constants import BOT_NAME

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


DEFAULT_SUMMARIZATION_TRIGGER = ("tokens", 170000)
DEFAULT_SUMMARIZATION_KEEP = ("messages", 6)


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


def create_explore_subagent() -> SubAgent:
    """
    Create the explore subagent.
    """
    return SubAgent(
        name="explore",
        description="""Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive analysis across multiple locations and naming conventions.""",  # noqa: E501
        system_prompt=explore_system_prompt,
        tools=[],  # empty tools list to avoid inheritance of tools from the parent agent
    )


def create_pipeline_debugger_subagent() -> SubAgent:
    """
    Create the pipeline debugger subagent.
    """
    return SubAgent(
        name="pipeline-debugger",
        description="""Specialized agent for investigating the latest CI pipeline/workflow for a merge/pull request. Use this when you need to investigate a pipeline failure and produce a concise RCA, or when the user explicitly requests pipeline investigation, debugging, or status checking.""",  # noqa: E501
        system_prompt=pipeline_debugger_system_prompt,
        tools=[pipeline_tool, job_logs_tool],
    )


async def create_daiv_agent(
    model_names: list[ModelName | str] = (settings.MODEL_NAME, settings.FALLBACK_MODEL_NAME),
    thinking_level: ThinkingLevel | None = settings.THINKING_LEVEL,
    *,
    runtime: RuntimeCtx,
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
        runtime: The runtime context.
        checkpointer: The checkpointer to use for the agent.
        store: The store to use for the agent.
        debug: Whether to enable debug mode for the agent.
        cache: Whether to enable cache for the agent.
        name: The name of the agent.

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

    backend = FilesystemBackend(root_dir=runtime.repo.working_dir, virtual_mode=True)

    tools = await MCPToolkit.get_tools()
    subagents = [create_explore_subagent()]
    subagent_middlewares = [
        TodoListMiddleware(),
        WebSearchMiddleware(),
        FilesystemMiddleware(backend=backend),
        SummarizationMiddleware(
            model=model, trigger=summarization_trigger, keep=summarization_keep, trim_tokens_to_summarize=None
        ),
        AnthropicPromptCachingMiddleware(),
        PatchToolCallsMiddleware(),
    ]

    if runtime.scope == "merge_request":
        subagent_middlewares.append(MergeRequestMiddleware())
        subagents.append(create_pipeline_debugger_subagent())

    if runtime.config.sandbox.enabled:
        subagent_middlewares.append(SandboxMiddleware())

    if fallback_models:
        subagent_middlewares.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))

    deepagent_middleware = [
        TodoListMiddleware(),
        WebSearchMiddleware(),
        FilesystemMiddleware(backend=backend),
        LongTermMemoryMiddleware(),
        SkillsMiddleware(repo_dir=Path(runtime.repo.working_dir), scope=runtime.scope),
        SubAgentMiddleware(
            default_model=model,
            default_tools=tools,
            default_middleware=subagent_middlewares,
            general_purpose_agent=True,
            subagents=subagents,
        ),
        SummarizationMiddleware(
            model=model, trigger=summarization_trigger, keep=summarization_keep, trim_tokens_to_summarize=None
        ),
        AnthropicPromptCachingMiddleware(),
        PatchToolCallsMiddleware(),
        dinamic_daiv_system_prompt,
    ]

    if supported_image_inputs:
        deepagent_middleware.append(InjectImagesMiddleware())

    if runtime.config.sandbox.enabled:
        deepagent_middleware.append(SandboxMiddleware())

    if fallback_models:
        deepagent_middleware.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))

    return create_agent(
        model,
        tools=tools,
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
    async with set_runtime_ctx(repo_id="srtab/daiv", ref="main", scope="merge_request", merge_request_id=45) as ctx:
        agent = await create_daiv_agent(runtime=ctx, model_names=[ModelName.CLAUDE_SONNET_4_5])
        response = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Why did the pipeline fail?"}]}, context=ctx
        )
        for message in response["messages"]:
            print(message.pretty_print())  # noqa: T201


if __name__ == "__main__":
    django.setup()
    asyncio.run(main())
