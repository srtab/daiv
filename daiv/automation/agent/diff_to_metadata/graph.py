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
from prompt_toolkit import HTML, PromptSession

from automation.agent import BaseAgent
from automation.agent.constants import AGENTS_MEMORY_PATH, ModelName
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from codebase.base import Scope
from codebase.context import RuntimeCtx, set_runtime_ctx
from codebase.utils import redact_diff_content

from .conf import settings
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
    return system_prompt + cast("str", system.format(current_date_time=timezone.now().strftime("%d %B, %Y")).content)


def create_diff_to_metadata_graph(
    model_names: Sequence[ModelName | str] = (settings.MODEL_NAME, settings.FALLBACK_MODEL_NAME),
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
    assert include_pr_metadata or include_commit_message, (
        "At least one of include_pr_metadata or include_commit_message must be True"
    )

    agent_path = Path(ctx.repo.working_dir)

    backend = FilesystemBackend(root_dir=agent_path.parent, virtual_mode=True)

    model = BaseAgent.get_model(model=model_names[0])
    fallback_models = [BaseAgent.get_model(model=model_name) for model_name in model_names[1:]]

    middleware = [
        MemoryMiddleware(
            backend=backend,
            sources=[f"/{agent_path.name}/{ctx.config.context_file_name}", f"/{agent_path.name}/{AGENTS_MEMORY_PATH}"],
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
            ChatPromptTemplate.from_messages([human_commit_message])
            | create_agent(
                model=model,
                tools=[],  # No tools are needed for this agent, it only uses the memory and the system prompt
                middleware=middleware,
                response_format=CommitMetadata,
                context_schema=RuntimeCtx,
            )
        ).with_config(run_name="CommitMessage")

    def _input_selector(x: dict[str, Any]) -> dict[str, str]:
        input_data = {}
        if include_pr_metadata:
            input_data["pr_metadata_diff"] = x.get("pr_metadata_diff", x.get("diff", ""))
        if include_commit_message:
            input_data["commit_message_diff"] = x.get("commit_message_diff", x.get("diff", ""))
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
        metadata={"include_pr_metadata": include_pr_metadata, "include_commit_message": include_commit_message},
    )


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
        diff_to_metadata_graph = create_diff_to_metadata_graph(ctx=ctx, model_names=[ModelName.CLAUDE_HAIKU_4_5])
        while True:
            user_input = await session.prompt_async()
            output = await diff_to_metadata_graph.ainvoke(
                {"diff": redact_diff_content(user_input, ctx.config.omit_content_patterns)},
                context=ctx,
                config={"configurable": {"thread_id": "1"}},
            )
            if output and "pr_metadata" in output:
                print(output["pr_metadata"].model_dump_json(indent=2))  # noqa: T201
            if output and "commit_message" in output:
                print(output["commit_message"].model_dump_json(indent=2))  # noqa: T201


if __name__ == "__main__":
    import asyncio

    import django

    django.setup()
    asyncio.run(main())
