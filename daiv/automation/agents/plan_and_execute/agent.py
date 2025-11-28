from __future__ import annotations

import logging
from textwrap import dedent
from typing import TYPE_CHECKING, Any, Literal

from django.utils import timezone

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelFallbackMiddleware,
    ModelRequest,
    ModelResponse,
    TodoListMiddleware,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.agents.middleware import AgentsMDMiddleware, AnthropicPromptCachingMiddleware, InjectImagesMiddleware
from automation.agents.tools.editing import FileEditingMiddleware
from automation.agents.tools.merge_request import MergeRequestMiddleware
from automation.agents.tools.navigation import FileNavigationMiddleware
from automation.agents.tools.sandbox import (
    BASH_TOOL_NAME,
    FORMAT_CODE_SYSTEM_PROMPT,
    FORMAT_CODE_TOOL_NAME,
    SandboxMiddleware,
)
from automation.agents.tools.toolkits import MCPToolkit
from automation.agents.tools.web_search import WebSearchMiddleware
from codebase.context import RuntimeCtx
from core.constants import BOT_NAME

from .conf import settings
from .prompts import execute_plan_human, execute_plan_system, plan_system, prepare_execute_plan_context
from .schemas import FinalizerOutput, FinishOutput
from .state import ExecutorState, PlanAndExecuteState
from .tools import review_code_changes_tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelCallResult
    from langgraph.runtime import Runtime

    from automation.agents.constants import ModelName


logger = logging.getLogger("daiv.agents")


class PlanMiddleware(AgentMiddleware):
    """
    Middleware to format the system prompt for the plan agent with a optional specialized prompt.
    """

    name = "plan_middleware"

    def __init__(self, *, specialized_planner_prompt: str | None = None):
        """
        Initialize the middleware.
        """
        self.specialized_planner_prompt = specialized_planner_prompt

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        """
        Format the system prompt for the plan agent with a optional specialized prompt.

        Args:
            request (ModelRequest): The request to the model.
            handler (Callable[[ModelRequest], ModelResponse]): The handler to call the model.

        Returns:
            ModelCallResult: The result of the model call.
        """
        system_prompt = plan_system.format(
            current_date_time=timezone.now().strftime("%d %B, %Y"),
            repository=request.runtime.context.repo_id,
            bot_name=BOT_NAME,
            bot_username=request.runtime.context.bot_username,
        ).content

        if self.specialized_planner_prompt:
            system_prompt += "\n\n" + self.specialized_planner_prompt

        request = request.override(system_prompt=system_prompt)

        return await handler(request)


class ExecutorMiddleware(AgentMiddleware):
    """
    Middleware to select the tools for the executor agent based on the tool calls.
    """

    name = "executor_middleware"

    def __init__(self) -> None:
        """
        Initialize the middleware.
        """
        self.tools = [review_code_changes_tool]

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
        preprocessed_context = prepare_execute_plan_context(state["plan_tasks"], state["relevant_files"])
        return {"messages": await prompt.aformat_messages(**preprocessed_context)}

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

    def __init__(
        self,
        *,
        skip_approval: bool = False,
        skip_format_code: bool = False,
        specialized_planner_prompt: str | None = None,
        planning_model_names: list[ModelName | str] = (
            settings.PLANNING_MODEL_NAME,
            settings.PLANNING_FALLBACK_MODEL_NAME,
        ),
        execution_model_names: list[ModelName | str] = (
            settings.EXECUTION_MODEL_NAME,
            settings.EXECUTION_FALLBACK_MODEL_NAME,
        ),
        **kwargs,
    ):
        """
        Initialize the agent.

        Args:
            skip_approval (bool): Whether to skip the approval step.
            skip_format_code (bool): Whether to skip the format code step.
            specialized_planner_prompt (str | None): The specialized planner prompt to use.
            planning_model_names (list[ModelName | str]): The names of the planning models to use.
            execution_model_names (list[ModelName | str]): The names of the execution models to use.
        """
        self.skip_approval = skip_approval
        self.skip_format_code = skip_format_code
        self.specialized_planner_prompt = specialized_planner_prompt
        self._planning_model = BaseAgent.get_model(
            model=planning_model_names[0], max_tokens=8_192, thinking_level=settings.PLANNING_THINKING_LEVEL
        )
        self._planning_fallback_models = [
            BaseAgent.get_model(model=model_name, thinking_level=settings.PLANNING_THINKING_LEVEL)
            for model_name in planning_model_names[1:]
        ]
        self._execution_model = BaseAgent.get_model(model=execution_model_names[0], max_tokens=8_192)
        self._execution_fallback_models = [
            BaseAgent.get_model(model=model_name, max_tokens=8_192) for model_name in execution_model_names[1:]
        ]
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
        model = BaseAgent.get_model(
            model=settings.PLANNING_MODEL_NAME, max_tokens=8_192, thinking_level=settings.PLANNING_THINKING_LEVEL
        )

        middlewares: list[AgentMiddleware] = [
            PlanMiddleware(specialized_planner_prompt=self.specialized_planner_prompt),
            FileNavigationMiddleware(),
            WebSearchMiddleware(),
            InjectImagesMiddleware(image_inputs_supported=model.profile.get("image_inputs", True)),
            AgentsMDMiddleware(),
            TodoListMiddleware(),
            ModelFallbackMiddleware(
                first_model=BaseAgent.get_model(
                    model=settings.PLANNING_FALLBACK_MODEL_NAME, thinking_level=settings.PLANNING_THINKING_LEVEL
                )
            ),
            AnthropicPromptCachingMiddleware(),
        ]

        if self._planning_fallback_models:
            middlewares.append(
                ModelFallbackMiddleware(self._planning_fallback_models[0], *self._planning_fallback_models[1:])
            )

        if runtime.context.merge_request_id:
            middlewares.append(MergeRequestMiddleware())

        if runtime.context.config.sandbox.enabled:
            middlewares.append(SandboxMiddleware(read_only_bash=True))

        planner_agent = create_agent(
            model=self._planning_model,
            tools=await MCPToolkit.get_tools(),
            store=runtime.store,
            checkpointer=False,
            context_schema=RuntimeCtx,
            response_format=ToolStrategy(FinalizerOutput),
            middleware=middlewares,
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
        middlewares: list[AgentMiddleware] = [
            ExecutorMiddleware(),
            FileNavigationMiddleware(),
            FileEditingMiddleware(),
            TodoListMiddleware(),
            ModelFallbackMiddleware(first_model=BaseAgent.get_model(model=settings.EXECUTION_FALLBACK_MODEL_NAME)),
            AnthropicPromptCachingMiddleware(),
        ]

        if runtime.context.config.sandbox.enabled:
            format_system_prompt = FORMAT_CODE_SYSTEM_PROMPT + dedent(
                """\
                **Formatting is non-blocking:** if cycles are exhausted after a prior PASS, proceed to Step 4 (non-abort) and report the formatting failure. Treat any `error:` as requiring a return to Step 2 (new cycle) to address the issues."""  # noqa: E501
            )
            middlewares.append(
                SandboxMiddleware(
                    include_format_code=bool(
                        not self.skip_format_code and runtime.context.config.sandbox.format_code_enabled
                    ),
                    format_system_prompt=format_system_prompt,
                )
            )

        if self._execution_fallback_models:
            middlewares.append(
                ModelFallbackMiddleware(self._execution_fallback_models[0], *self._execution_fallback_models[1:])
            )

        executor_agent = create_agent(
            model=self._execution_model,
            state_schema=ExecutorState,
            context_schema=RuntimeCtx,
            store=runtime.store,
            middleware=middlewares,
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
