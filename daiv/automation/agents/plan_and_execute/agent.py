from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from django.utils import timezone

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, TodoListMiddleware, dynamic_prompt
from langchain.agents.structured_output import ToolStrategy
from langchain_anthropic.middleware.prompt_caching import AnthropicPromptCachingMiddleware
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.agents.middleware import InjectImagesMiddleware
from automation.agents.tools.navigation import READ_MAX_LINES
from automation.agents.tools.sandbox import BASH_TOOL_NAME, FORMAT_CODE_TOOL_NAME, SandboxMiddleware
from automation.agents.tools.toolkits import (
    FileEditingToolkit,
    FileNavigationToolkit,
    MCPToolkit,
    MergeRequestToolkit,
    WebSearchToolkit,
)
from codebase.context import RuntimeCtx
from core.constants import BOT_NAME

from .conf import settings
from .prompts import execute_plan_human, execute_plan_system, plan_system
from .schemas import FinalizerOutput, FinishOutput
from .state import ExecutorState, PlanAndExecuteState
from .tools import plan_think_tool, review_code_changes_tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from langchain.agents.middleware.types import ModelCallResult
    from langchain_core.tools import BaseTool
    from langgraph.runtime import Runtime


logger = logging.getLogger("daiv.agents")


def get_agents_md_content(repo_dir: Path, context_file_name: str | None) -> str | None:
    """
    Get the agent instructions from the AGENTS.md file case insensitive.
    If multiple files are found, return the first one.
    If the file is too long, return the first `max_lines` lines.

    Args:
        context_file_name (str | None): The name of the context file.

    Returns:
        str | None: The agents instructions from the AGENTS.md file.
    """
    if not context_file_name:
        return None

    for path in repo_dir.glob(context_file_name, case_sensitive=False):
        if path.is_file() and path.name.endswith(".md"):
            return "\n".join(path.read_text().splitlines()[:READ_MAX_LINES])
    return None


@dynamic_prompt
def plan_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic prompt for the plan system.

    Args:
        request (ModelRequest): The request to the model.

    Returns:
        str: The dynamic prompt for the plan system.
    """
    tools_names = [tool.name for tool in request.tools]
    return plan_system.format(
        current_date_time=timezone.now().strftime("%d %B, %Y"),
        repository=request.runtime.context.repo_id,
        agents_md_content=get_agents_md_content(
            request.runtime.context.repo_dir, request.runtime.context.config.context_file_name
        ),
        tools_names=tools_names,
        bot_name=BOT_NAME,
        bot_username=request.runtime.context.bot_username,
        commands_enabled=BASH_TOOL_NAME in tools_names,
    ).content


class ExecutorMiddleware(AgentMiddleware):
    """
    Middleware to select the tools for the executor agent based on the tool calls.
    """

    name = "executor_middleware"

    async def abefore_agent(self, state: ExecutorState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Inject the change plan into the messages as a human message.

        Args:
            state (ExecutorState): The state of the executor agent.
            runtime (Runtime[RuntimeCtx]): The runtime context containing the repository id.

        Returns:
            dict[str, Any] | None: The state updates with the formatted messages.
        """
        prompt = ChatPromptTemplate.from_messages([execute_plan_human])
        return {"messages": await prompt.aformat_messages(**state)}

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        """
        Select the tools for the executor agent based on the tool calls.

        Args:
            request (ModelRequest): The request to the model.
            handler (Callable[[ModelRequest], ModelResponse]): The handler to call the model.

        Returns:
            ModelCallResult: The result of the model call.
        """
        tools_names = [tool.name for tool in request.tools]
        request.system_prompt = execute_plan_system.format(
            current_date_time=timezone.now().strftime("%d %B, %Y"),
            repository=request.runtime.context.repo_id,
            commands_enabled=BASH_TOOL_NAME in tools_names,
            format_code_enabled=FORMAT_CODE_TOOL_NAME in tools_names,
            tools_names=tools_names,
        ).content
        return await handler(request)


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
        super().__init__(**kwargs)

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(PlanAndExecuteState, context_schema=RuntimeCtx)

        workflow.add_node("plan", self.plan)
        workflow.add_node("plan_approval", self.plan_approval)
        workflow.add_node("execute_plan", self.execute_plan)
        workflow.set_entry_point("plan")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def plan(
        self, state: PlanAndExecuteState, runtime: Runtime[RuntimeCtx]
    ) -> Command[Literal["plan_approval", "__end__"]]:
        """
        Node to plan the steps to follow.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            Command[Literal["plan_approval", "__end__"]]: The next step in the workflow.
        """

        all_tools: list[BaseTool] = (
            (await MCPToolkit.get_tools())
            + FileNavigationToolkit.get_tools()
            + WebSearchToolkit.get_tools()
            + [plan_think_tool]
        )

        if runtime.context.merge_request_id:
            all_tools.extend(MergeRequestToolkit.get_tools())

        conditional_middlewares: list[AgentMiddleware] = []
        if runtime.context.config.sandbox.enabled:
            conditional_middlewares.append(SandboxMiddleware(read_only_bash=True))

        planner_agent = create_agent(
            model=BaseAgent.get_model(
                model=settings.PLANNING_MODEL_NAME, max_tokens=8_192, thinking_level=settings.PLANNING_THINKING_LEVEL
            ),
            tools=all_tools,
            store=runtime.store,
            checkpointer=False,
            context_schema=RuntimeCtx,
            response_format=ToolStrategy(FinalizerOutput),
            middleware=[
                plan_system_prompt,
                InjectImagesMiddleware(),
                *conditional_middlewares,
                AnthropicPromptCachingMiddleware(),
            ],
            name="planner_agent",
        )

        response = await planner_agent.ainvoke(
            {"messages": state["messages"]},
            config={"recursion_limit": settings.PLANNING_RECURSION_LIMIT},
            context=runtime.context,
        )

        if "structured_response" not in response:
            return Command(goto=END, update={"messages": [response["messages"][-1]]})

        structured_response: FinalizerOutput = response["structured_response"]

        if structured_response.type == "plan":
            logger.info("[plan] The plan to execute: %s", repr(structured_response.changes))
            return Command(goto="plan_approval", update={"plan_tasks": structured_response.changes})

        elif structured_response.type == "clarify":
            logger.info("[clarify] Clarifying the inspection: %s", structured_response.questions)
            return Command(goto=END, update={"plan_questions": structured_response.questions})

        elif structured_response.type == "complete":
            logger.info("[complete] No changes needed: %s", structured_response.message)
            return Command(goto=END, update={"no_changes_needed": structured_response.message})

        raise ValueError(f"Unexpected structured output type: {structured_response.type}")

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

    async def execute_plan(
        self, state: PlanAndExecuteState, runtime: Runtime[RuntimeCtx]
    ) -> Command[Literal["__end__"]]:
        """
        Node to execute the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        conditional_middlewares: list[AgentMiddleware] = []
        if runtime.context.config.sandbox.enabled:
            conditional_middlewares.append(
                SandboxMiddleware(
                    include_format_code=bool(
                        not self.skip_format_code and runtime.context.config.sandbox.format_code_enabled
                    )
                )
            )

        executor_agent = create_agent(
            model=BaseAgent.get_model(model=settings.EXECUTION_MODEL_NAME, max_tokens=8_192),
            state_schema=ExecutorState,
            context_schema=RuntimeCtx,
            tools=(FileNavigationToolkit.get_tools() + FileEditingToolkit.get_tools() + [review_code_changes_tool]),
            store=runtime.store,
            middleware=[
                ExecutorMiddleware(),
                *conditional_middlewares,
                TodoListMiddleware(),
                AnthropicPromptCachingMiddleware(),
            ],
            response_format=ToolStrategy(FinishOutput),
            checkpointer=False,
            name="executor_agent",
        )

        response = await executor_agent.ainvoke(
            {
                "plan_tasks": state["plan_tasks"],
                "relevant_files": list({
                    file_path for task in state["plan_tasks"] for file_path in task.relevant_files
                }),
            },
            config={"recursion_limit": settings.EXECUTION_RECURSION_LIMIT},
            context=runtime.context,
        )

        structured_response: FinishOutput = response["structured_response"]

        return Command(goto=END, update={"messages": [AIMessage(content=structured_response.message)]})
