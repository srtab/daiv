from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from django.utils import timezone

from deepagents.backends import FilesystemBackend
from deepagents.graph import create_agent
from deepagents.middleware.memory import MemoryMiddleware
from langchain.agents.middleware import ModelFallbackMiddleware, dynamic_prompt
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableParallel

from automation.agent import BaseAgent
from automation.agent.constants import AGENTS_MEMORY_PATH, ModelName
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from codebase.context import RuntimeCtx
from core.site_settings import site_settings

from .prompts import human_commit_message, human_pr_metadata, system
from .schemas import CommitMetadata, PullRequestMetadata

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.runnables import Runnable


@dynamic_prompt
def dynamic_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic system prompt for the changes metadata agent.
    """
    system_prompt = ""
    if request.system_prompt:
        system_prompt = request.system_prompt + "\n\n"
    return system_prompt + cast("str", system.format(current_date=timezone.now().strftime("%d %B, %Y")).content)


def create_diff_to_metadata_graph(
    model_names: Sequence[ModelName | str] | None = None,
    *,
    ctx: RuntimeCtx,
    include_pr_metadata: bool = True,
    include_commit_message: bool = True,
) -> Runnable:
    """
    Create a graph to describe changes to feed into a pull request and optionally a commit message.

    Args:
        model: The model to use for the agent.
        ctx: The runtime context.

    Returns:
        The PR metadata graph.
    """
    if model_names is None:
        model_names = (site_settings.diff_to_metadata_model_name, site_settings.diff_to_metadata_fallback_model_name)

    assert include_pr_metadata or include_commit_message, (
        "At least one of include_pr_metadata or include_commit_message must be True"
    )

    agent_path = Path(ctx.gitrepo.working_dir)

    backend = FilesystemBackend(root_dir=agent_path.parent, virtual_mode=True)

    model = BaseAgent.get_model(model=model_names[0])
    fallback_models = [BaseAgent.get_model(model=model_name) for model_name in model_names[1:]]

    middleware = [
        MemoryMiddleware(
            backend=backend,
            sources=[f"/{agent_path.name}/{ctx.config.context_file_name}", f"/{agent_path.name}/{AGENTS_MEMORY_PATH}"],
            add_cache_control=True,
        ),
        AnthropicPromptCachingMiddleware(),
        dynamic_system_prompt,
    ]

    if fallback_models:
        middleware.append(ModelFallbackMiddleware(fallback_models[0], *fallback_models[1:]))

    graphs: dict[str, Runnable] = {}

    if include_pr_metadata:
        graphs["pr_metadata"] = (
            ChatPromptTemplate.from_messages([human_pr_metadata]).partial(extra_context="")
            | create_agent(
                model=model,
                tools=[],  # No tools are needed for this agent, it only uses the memory and the system prompt
                middleware=middleware,
                response_format=PullRequestMetadata,
                context_schema=RuntimeCtx,
            )
        ).with_config(run_name="PRMetadata")

    if include_commit_message:
        graphs["commit_message"] = (
            ChatPromptTemplate.from_messages([human_commit_message]).partial(extra_context="")
            | create_agent(
                model=model,
                tools=[],  # No tools are needed for this agent, it only uses the memory and the system prompt
                middleware=middleware,
                response_format=CommitMetadata,
                context_schema=RuntimeCtx,
            )
        ).with_config(run_name="CommitMessage")

    def _input_selector(x: dict[str, Any]) -> dict[str, str]:
        input_data: dict[str, str] = {}
        if include_pr_metadata:
            input_data["pr_metadata_diff"] = x.get("pr_metadata_diff", x.get("diff", ""))
        if include_commit_message:
            input_data["commit_message_diff"] = x.get("commit_message_diff", x.get("diff", ""))
        if extra_context := x.get("extra_context", ""):
            input_data["extra_context"] = extra_context
        return input_data

    def _output_selector(x: dict[str, Any]) -> dict[str, PullRequestMetadata | CommitMetadata]:
        output: dict[str, PullRequestMetadata | CommitMetadata] = {}
        if include_pr_metadata and "pr_metadata" in x:
            output["pr_metadata"] = x["pr_metadata"]["structured_response"]
        if include_commit_message and "commit_message" in x:
            output["commit_message"] = x["commit_message"]["structured_response"]
        return output

    run_name = "DiffToMetadata"
    return (RunnableLambda(_input_selector) | RunnableParallel(graphs) | RunnableLambda(_output_selector)).with_config(
        run_name=run_name,
        tags=[run_name],
        # `emit-messages: False` is honored by ag_ui_langgraph and silences the
        # subagents' text + reasoning frames so partial JSON from the structured
        # response never bleeds into the chat. We *do* let TOOL_CALL_* events
        # through: the chat client recognizes the `PullRequestMetadata` and
        # `CommitMetadata` tool names and renders them as inline progress chips
        # ("Creating merge request…" / "Committing changes…") instead of raw
        # tool cards.
        metadata={
            "include_pr_metadata": include_pr_metadata,
            "include_commit_message": include_commit_message,
            "emit-messages": False,
        },
    )
