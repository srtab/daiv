from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from django.utils import timezone

from langchain.agents import create_agent
from langchain_anthropic.middleware.prompt_caching import AnthropicPromptCachingMiddleware
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.agents.middleware import InjectImagesMiddleware
from automation.agents.nodes import apply_format_code_node
from automation.agents.tools import THINK_TOOL_NAME, think_tool
from automation.agents.tools.navigation import NAVIGATION_TOOLS, READ_MAX_LINES, READ_TOOL_NAME
from automation.agents.tools.toolkits import (
    FileEditingToolkit,
    FileNavigationToolkit,
    MCPToolkit,
    MergeRequestToolkit,
    SandboxToolkit,
    WebSearchToolkit,
)
from automation.utils import has_file_changes
from codebase.context import RuntimeCtx, get_runtime_ctx
from core.constants import BOT_LABEL, BOT_NAME

from .conf import settings
from .prompts import execute_plan_human, execute_plan_system, plan_system
from .state import ExecuteState, PlanAndExecuteState
from .tools import clarify_tool, complete_tool, plan_think_tool, plan_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models import LanguageModelLike
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import BaseTool
    from langgraph.runtime import ContextT, Runtime


logger = logging.getLogger("daiv.agents")


async def prepare_plan_model_and_tools(
    merge_request_id: int | None = None,
) -> tuple[Callable[[PlanAndExecuteState, Runtime[ContextT]], LanguageModelLike], list[BaseTool]]:
    """
    Wrapper for the plan_model function.

    Returns:
        tuple[Callable[[PlanAndExecuteState, Runtime[ContextT]], LanguageModelLike], list[BaseTool]]:
            The callable to prepare the model and the tools.
    """
    mcp_tools = await MCPToolkit.get_tools()
    file_navigation_tools = FileNavigationToolkit.get_tools()
    web_search_tools = WebSearchToolkit.get_tools()

    base_tools: list[BaseTool] = mcp_tools + file_navigation_tools + web_search_tools + [plan_think_tool, clarify_tool]

    if merge_request_id:
        base_tools.extend(MergeRequestToolkit.get_tools())

    def plan_model(state: PlanAndExecuteState, runtime: Runtime[ContextT]) -> LanguageModelLike:
        """
        Prepare the model for the planning step with the correct tools at each stage of the conversation.

        Force the sequence when possible to avoid the agent to call tools out of order.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            runtime (Runtime[ContextT]): The runtime of the agent.

        Returns:
            LanguageModelLike: The prepared model.
        """
        nonlocal base_tools

        tools = base_tools.copy()

        # only add the plan_tool and complete_tool tools if at least one of the navigation tool was called
        # ensure that the agent has collected context from the codebase
        if any(
            message
            for message in state["messages"]
            if message.type == "tool" and message.status == "success" and message.name in NAVIGATION_TOOLS
        ):
            tools += [plan_tool, complete_tool]

        return BaseAgent.get_model(
            model=settings.PLANNING_MODEL_NAME, max_tokens=8_192, thinking_level=settings.PLANNING_THINKING_LEVEL
        ).bind_tools(tools)

    return plan_model, base_tools + [plan_tool, complete_tool]


async def prepare_execute_model_and_tools() -> tuple[
    Callable[[PlanAndExecuteState, Runtime[ContextT]], LanguageModelLike], list[BaseTool]
]:
    """
    Wrapper for the execute_model function.

    Returns:
        tuple[Callable[[PlanAndExecuteState, Runtime[ContextT]], LanguageModelLike], list[BaseTool]]:
            The callable to prepare the model and the tools.
    """
    file_navigation_tools = FileNavigationToolkit.get_tools()
    file_editing_tools = FileEditingToolkit.get_tools()
    sandbox_tools = await SandboxToolkit.get_tools()

    base_tools: list[BaseTool] = file_navigation_tools + [think_tool]

    def execute_model(state: PlanAndExecuteState, runtime: Runtime[ContextT]) -> LanguageModelLike:
        """
        Prepare the model for the execution step with the correct tools at each stage of the conversation.

        Force the sequence to avoid the agent to call tools out of order:
        - `read` (fetching the relevant files)
            -> `think` (planning the edits)
            -> `file_editing` or `sandbox` (applying the edits or running commands).

        Args:
            state (PlanAndExecuteState): The state of the agent.
            runtime (Runtime[ContextT]): The runtime of the agent.

        Returns:
            LanguageModelLike: The prepared model.
        """
        nonlocal file_editing_tools
        nonlocal sandbox_tools
        nonlocal base_tools

        tools = base_tools.copy()

        tool_choice = "auto"

        # if the agent has not called any tool, we force the model to read the files
        # careful, this only work if thinking is disabled
        if len(state["messages"]) <= 2:
            tool_choice = READ_TOOL_NAME

        # only add the file_editing and sandbox tools if the agent has called the think tool
        if any(
            message
            for message in state["messages"]
            if message.type == "tool" and message.status == "success" and message.name in THINK_TOOL_NAME
        ):
            tools += file_editing_tools + sandbox_tools

        return BaseAgent.get_model(model=settings.EXECUTION_MODEL_NAME, max_tokens=8_192).bind_tools(
            tools, tool_choice=tool_choice
        )

    return execute_model, base_tools + file_editing_tools + sandbox_tools


class PlanAndExecuteAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to plan and execute a task.
    """

    def __init__(self, *, skip_approval: bool = False, skip_format_code: bool = False, **kwargs):
        """
        Initialize the agent.

        Args:
            skip_approval (bool): Whether to skip the approval step.
            skip_format_code (bool): Whether to skip the format code step.
        """
        self.skip_approval = skip_approval
        self.skip_format_code = skip_format_code
        self.ctx = get_runtime_ctx()
        super().__init__(**kwargs)

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(PlanAndExecuteState)

        workflow.add_node("plan", self.plan)
        workflow.add_node("plan_approval", self.plan_approval)

        workflow.add_node("execute_plan", self.execute_plan)

        if not self.skip_format_code:
            workflow.add_node("apply_format_code", self.apply_format_code)

        workflow.set_entry_point("plan")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def plan(
        self, state: PlanAndExecuteState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["plan_approval", "__end__"]]:
        """
        Node to plan the steps to follow.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Command[Literal["plan_approval", "__end__"]]: The next step in the workflow.
        """
        plan_model_fn, all_tools = await prepare_plan_model_and_tools(self.ctx.merge_request_id)

        react_agent = create_agent(
            model=plan_model_fn(state, None),  # TODO: migrate to v1 langchain middleware
            tools=all_tools,
            store=store,
            checkpointer=False,
            context_schema=RuntimeCtx,
            middleware=[
                InjectImagesMiddleware(),
                AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            ],
            system_prompt=(
                await plan_system.aformat(  # TODO: migrate to v1 langchain middleware
                    current_date_time=timezone.now().strftime("%d %B, %Y"),
                    repository=self.ctx.repo_id,
                    agents_md_content=self._get_agent_md_content(),
                    tools_names=[tool.name for tool in all_tools],
                    bot_name=BOT_NAME,
                    bot_username=config["configurable"].get("bot_username", BOT_LABEL),
                    commands_enabled=self.ctx.config.sandbox.enabled,
                )
            ).content,
            name="planner_react_agent",
        )

        response = await react_agent.ainvoke(
            {"messages": state["messages"]},
            config={"recursion_limit": settings.PLANNING_RECURSION_LIMIT},
            context=self.ctx,
        )

        # At this point, the agent should have called one of the finalize tools.
        last_message_artifact = response["messages"][-1].artifact

        if "plan_tasks" in last_message_artifact:
            return Command(goto="plan_approval", update={"plan_tasks": last_message_artifact["plan_tasks"]})
        elif "plan_questions" in last_message_artifact:
            return Command(goto=END, update={"plan_questions": last_message_artifact["plan_questions"]})
        elif "no_changes_needed" in last_message_artifact:
            return Command(goto=END, update={"no_changes_needed": last_message_artifact["no_changes_needed"]})
        else:
            logger.warning("The agent didn't call any tool and can't return a formatted response.")

        # If the agent don't call any finalize tool (theoretically impossible), the workflow will end.
        return Command(goto=END)

    async def plan_approval(self, state: PlanAndExecuteState) -> Command[Literal["execute_plan"]]:
        """
        Request human approval of the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.

        Returns:
            Command[Literal["execute_plan"]]: The next step in the workflow.
        """
        if not self.skip_approval:
            interrupt({
                "plan_tasks": state.get("plan_tasks"),
                "plan_questions": state.get("plan_questions"),
                "no_changes_needed": state.get("no_changes_needed"),
            })

        return Command(goto="execute_plan")

    async def execute_plan(self, state: PlanAndExecuteState, store: BaseStore) -> Command[Literal["__end__"]]:
        """
        Node to execute the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Command[Literal["apply_format_code", "__end__"]]: The next step in the workflow.
        """
        execute_model_fn, all_tools = await prepare_execute_model_and_tools()

        executor_agent = ChatPromptTemplate.from_messages([execute_plan_human]) | create_agent(
            model=execute_model_fn(state, None),
            state_schema=ExecuteState,
            tools=all_tools,
            store=store,
            middleware=[AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")],
            system_prompt=(
                await execute_plan_system.aformat(
                    current_date_time=timezone.now().strftime("%d %B, %Y"),
                    repository=self.ctx.repo_id,
                    commands_enabled=self.ctx.config.sandbox.enabled,
                    tools_names=[tool.name for tool in all_tools],
                )
            ).content,
            checkpointer=False,
            name="executor_react_agent",
        )

        response = await executor_agent.ainvoke(
            {
                "plan_tasks": state["plan_tasks"],
                "relevant_files": list({
                    file_path for task in state["plan_tasks"] for file_path in task.relevant_files
                }),
            },
            config={"recursion_limit": settings.EXECUTION_RECURSION_LIMIT},
        )

        if not self.skip_format_code and await has_file_changes(self.store):
            return Command(goto="apply_format_code")
        return Command(goto=END, update={"no_changes_needed": response["messages"][-1].content})

    async def apply_format_code(self, state: PlanAndExecuteState, store: BaseStore) -> Command[Literal["__end__"]]:
        """
        Apply format code to the file changes made by the agent.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        await apply_format_code_node(store)
        return Command(goto=END)

    def _get_agent_md_content(self, max_lines: int = READ_MAX_LINES) -> str | None:
        """
        Get the agent instructions from the AGENTS.md file case insensitive.
        If multiple files are found, return the first one.
        If the file is too long, return the first `max_lines` lines.

        Returns:
            str | None: The agent instructions.
        """
        if not self.ctx.config.context_file_name:
            return None

        for path in self.ctx.repo_dir.glob(self.ctx.config.context_file_name, case_sensitive=False):
            if path.is_file() and path.name.endswith(".md"):
                return "\n".join(path.read_text().splitlines()[:max_lines])
        return None
