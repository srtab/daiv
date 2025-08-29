from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from django.utils import timezone

from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig, RunnableLambda  # noqa: TC002
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.agents.nodes import apply_format_code_node
from automation.agents.schemas import ImageTemplate
from automation.agents.tools import think
from automation.agents.tools.navigation import NAVIGATION_TOOLS
from automation.agents.tools.toolkits import (
    FileEditingToolkit,
    FileNavigationToolkit,
    MCPToolkit,
    SandboxToolkit,
    WebSearchToolkit,
)
from automation.utils import has_file_changes
from codebase.context import get_repository_ctx
from core.constants import BOT_LABEL, BOT_NAME

from .conf import settings
from .prompts import execute_plan_human, execute_plan_system, image_extractor_human, image_extractor_system, plan_system
from .schemas import ImageURLExtractorOutput
from .state import ExecuteState, PlanAndExecuteState
from .tools import (
    FINALIZE_WITH_PLAN_NAME,
    PLAN_THINK_NAME,
    finalize_with_plan,
    finalize_with_targeted_questions,
    plan_think,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models import LanguageModelLike
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.prompts import SystemMessagePromptTemplate
    from langchain_core.tools import BaseTool
    from langgraph.runtime import ContextT, Runtime


logger = logging.getLogger("daiv.agents")


async def _image_extrator_post_process(output: ImageURLExtractorOutput) -> list[ImageTemplate]:
    """
    Post-process the extracted images.

    Args:
        output (ImageURLExtractorOutput): The extracted images.

    Returns:
        list[ImageTemplate]: The processed images ready to be used on prompt templates.
    """
    return await ImageTemplate.from_images(output.images)


def wrapped_prepare_plan_model(
    model: BaseChatModel, tools: list[BaseTool]
) -> Callable[[PlanAndExecuteState, Runtime[ContextT]], LanguageModelLike]:
    """
    Wrapper for the prepare_plan_model function.

    Args:
        model (BaseChatModel): The model to use for the planning step.
        tools (list[BaseTool]): The tools to use for the planning step.

    Returns:
        Callable[[PlanAndExecuteState, Runtime[ContextT]], LanguageModelLike]: The prepared model.
    """
    base_tools = [tool for tool in tools if tool.name != FINALIZE_WITH_PLAN_NAME]

    def prepare_plan_model(state: PlanAndExecuteState, runtime: Runtime[ContextT]) -> LanguageModelLike:
        """
        Prepare the model for the planning step with the correct tools at each stage of the conversation.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            runtime (Runtime[ContextT]): The runtime of the agent.

        Returns:
            LanguageModelLike: The prepared model.
        """
        nonlocal base_tools
        nonlocal model

        tools = base_tools.copy()
        tool_choice = "any"

        # if the agent has not called any tool, we force the model to think
        if len(state["messages"]) <= 2:
            tool_choice = PLAN_THINK_NAME

        # only add the finalize_with_plan tool if at least one of the navigation tool was called
        if any(
            message
            for message in state["messages"]
            if message.type == "tool" and message.status == "success" and message.name in NAVIGATION_TOOLS
        ):
            tools += [finalize_with_plan]

        return model.bind_tools(tools, tool_choice=tool_choice)

    return prepare_plan_model


class PlanAndExecuteAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to plan and execute a task.
    """

    def __init__(
        self,
        *,
        skip_approval: bool = False,
        skip_format_code: bool = False,
        plan_system_template: SystemMessagePromptTemplate | None = None,
        **kwargs,
    ):
        """
        Initialize the agent.

        Args:
            skip_approval (bool): Whether to skip the approval step.
            skip_format_code (bool): Whether to skip the format code step.
            plan_system_template (SystemMessagePromptTemplate): The system prompt template for the planning step.
        """
        self.plan_system_template = plan_system_template or plan_system
        self.skip_approval = skip_approval
        self.skip_format_code = skip_format_code
        self.ctx = get_repository_ctx()
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

    async def pre_plan(self, state: PlanAndExecuteState) -> Command[Literal["plan"]]:
        """
        Prepare the data for the plan node before the planning step.
        This node will extract the images from the messages.

        Args:
            state (PlanAndExecuteState): The state of the agent.

        Returns:
            Command[Literal["plan"]]: The next step in the workflow.
        """

        image_extractor = (
            ChatPromptTemplate.from_messages([image_extractor_system, image_extractor_human])
            | BaseAgent.get_model(model=settings.IMAGE_EXTRACTOR_MODEL_NAME).with_structured_output(
                ImageURLExtractorOutput
            )
            | RunnableLambda(_image_extrator_post_process, name="post_process_extracted_images")
        )

        latest_message = state["messages"][-1]
        extracted_images = await image_extractor.ainvoke({"body": latest_message.content})

        return Command[Literal["plan"]](
            goto="plan",
            update={
                "messages": [
                    RemoveMessage(id=latest_message.id),
                    HumanMessage([latest_message.content] + extracted_images),
                ]
            },
        )

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
        all_tools = await self._get_plan_tools()
        model = BaseAgent.get_model(
            model=settings.PLANNING_MODEL_NAME, max_tokens=8_192, thinking_level=settings.PLANNING_THINKING_LEVEL
        )
        react_agent = create_react_agent(
            model=wrapped_prepare_plan_model(model, all_tools),
            tools=all_tools,
            store=store,
            checkpointer=False,
            prompt=ChatPromptTemplate.from_messages([
                self.plan_system_template,
                MessagesPlaceholder("messages"),
            ]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"),
                repository=self.ctx.repo_id,
                tools_names=[tool.name for tool in all_tools],
                bot_name=BOT_NAME,
                bot_username=config["configurable"].get("bot_username", BOT_LABEL),
                commands_enabled=self.ctx.config.commands.enabled(),
                role=self.plan_system_template.prompt.partial_variables.get("role", ""),
                before_workflow=self.plan_system_template.prompt.partial_variables.get("before_workflow", ""),
                after_rules=self.plan_system_template.prompt.partial_variables.get("after_rules", ""),
            ),
            name="Planner",
            version="v2",
        ).with_config(RunnableConfig(recursion_limit=settings.RECURSION_LIMIT))

        response = await react_agent.ainvoke({"messages": state["messages"]})

        # At this point, the agent should have called one of the finalize tools.
        last_message_artifact = response["messages"][-1].artifact

        if "plan_tasks" in last_message_artifact:
            return Command(goto="plan_approval", update={"plan_tasks": last_message_artifact["plan_tasks"]})
        elif "plan_questions" in last_message_artifact:
            return Command(goto=END, update={"plan_questions": last_message_artifact["plan_questions"]})
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
            interrupt({"plan_tasks": state.get("plan_tasks"), "plan_questions": state.get("plan_questions")})

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
        all_tools = await self._get_execute_tools()

        react_agent = create_react_agent(
            BaseAgent.get_model(model=settings.EXECUTION_MODEL_NAME),
            state_schema=ExecuteState,
            tools=all_tools,
            store=store,
            prompt=ChatPromptTemplate.from_messages([
                execute_plan_system,
                HumanMessagePromptTemplate.from_template(execute_plan_human, "jinja2"),
                MessagesPlaceholder("messages"),
            ]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"),
                commands_enabled=self.ctx.config.commands.enabled(),
                tools_names=[tool.name for tool in all_tools],
            ),
            checkpointer=False,  # Disable checkpointer to avoid storing the execution in the store
            name="PlanExecuter",
            version="v2",
        ).with_config(RunnableConfig(recursion_limit=settings.RECURSION_LIMIT))

        await react_agent.ainvoke({
            "plan_tasks": state["plan_tasks"],
            "relevant_files": list({file_path for task in state["plan_tasks"] for file_path in task.relevant_files}),
        })

        if not self.skip_format_code and await has_file_changes(self.store):
            return Command(goto="apply_format_code")
        return Command(goto=END)

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

    async def _get_plan_tools(self) -> list[BaseTool]:
        """
        Get the tools for the planning step.

        Returns:
            list[BaseTool]: The tools for the planning step.
        """
        mcp_tools = await MCPToolkit.get_tools()
        repository_tools = FileNavigationToolkit.get_tools()
        web_search_tools = WebSearchToolkit.get_tools()
        return (
            repository_tools
            + web_search_tools
            + mcp_tools
            + [plan_think, finalize_with_plan, finalize_with_targeted_questions]
        )

    async def _get_execute_tools(self) -> list[BaseTool]:
        """
        Get the tools for the execution step.

        Args:
            commands_enabled (bool): Whether to include the sandbox tools.

        Returns:
            list[BaseTool]: The tools for the execution step.
        """
        repository_tools = FileEditingToolkit.get_tools()
        sandbox_tools = await SandboxToolkit.get_tools()
        return repository_tools + sandbox_tools + [think]
