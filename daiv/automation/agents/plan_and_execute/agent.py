from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig  # noqa: TC002
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.agents.nodes import apply_format_code
from automation.tools import think
from automation.tools.toolkits import (
    MCPToolkit,
    ReadRepositoryToolkit,
    SandboxToolkit,
    WebSearchToolkit,
    WriteRepositoryToolkit,
)
from automation.utils import file_changes_namespace
from core.constants import BOT_NAME

from .conf import settings
from .prompts import execute_plan_human, execute_plan_system, plan_system
from .state import ExecuteState, PlanAndExecuteConfig, PlanAndExecuteState
from .tools import complete_task

if TYPE_CHECKING:
    from langchain_core.prompts import SystemMessagePromptTemplate

logger = logging.getLogger("daiv.agents")


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
        super().__init__(**kwargs)

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(PlanAndExecuteState, config_schema=PlanAndExecuteConfig)

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
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["plan_approval", "__end__"]]: The next step in the workflow.
        """
        mcp_tools = (await MCPToolkit.create_instance()).get_tools()
        repository_tools = ReadRepositoryToolkit.create_instance().get_tools()
        web_search_tools = WebSearchToolkit.create_instance().get_tools()

        react_agent = create_react_agent(
            self.get_model(
                model=settings.PLANNING_MODEL_NAME, max_tokens=8_192, thinking_level=settings.PLANNING_THINKING_LEVEL
            ),
            tools=repository_tools + web_search_tools + mcp_tools + [think, complete_task],
            store=store,
            checkpointer=False,  # Disable checkpointer to avoid storing the plan in the store
            prompt=ChatPromptTemplate.from_messages([
                self.plan_system_template,
                MessagesPlaceholder("messages"),
            ]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"),
                mcp_tools_names=[tool.name for tool in mcp_tools],
                bot_name=BOT_NAME,
                bot_username=config["configurable"]["bot_username"],
                commands_enabled=config["configurable"]["commands_enabled"],
            ),
            name="Planner",
            version="v2",
        ).with_config(RunnableConfig(recursion_limit=settings.RECURSION_LIMIT))

        await react_agent.ainvoke({"messages": state["messages"]})

        # The agent should call the complete_task tool to determine the next action to take.
        # If the agent don't call the complete_task tool, the workflow will end.
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

    async def execute_plan(
        self, state: PlanAndExecuteState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Node to execute the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["apply_format_code", "__end__"]]: The next step in the workflow.
        """
        repository_tools = WriteRepositoryToolkit.create_instance().get_tools()
        sandbox_tools = (
            SandboxToolkit.create_instance().get_tools() if config["configurable"]["commands_enabled"] else []
        )

        react_agent = create_react_agent(
            self.get_model(model=settings.EXECUTION_MODEL_NAME),
            state_schema=ExecuteState,
            tools=repository_tools + sandbox_tools + [think],
            store=store,
            prompt=ChatPromptTemplate.from_messages([
                execute_plan_system,
                HumanMessagePromptTemplate.from_template(execute_plan_human, "jinja2"),
                MessagesPlaceholder("messages"),
            ]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"),
                commands_enabled=config["configurable"]["commands_enabled"],
            ),
            checkpointer=False,  # Disable checkpointer to avoid storing the execution in the store
            name="PlanExecuter",
            version="v2",
        ).with_config(RunnableConfig(recursion_limit=settings.RECURSION_LIMIT))

        await react_agent.ainvoke({
            "plan_tasks": state["plan_tasks"],
            "relevant_files": list({file_path for task in state["plan_tasks"] for file_path in task.relevant_files}),
        })

        if not self.skip_format_code and await store.asearch(
            file_changes_namespace(config["configurable"]["source_repo_id"], config["configurable"]["source_ref"]),
            limit=1,
        ):
            return Command(goto="apply_format_code")
        return Command(goto=END)

    async def apply_format_code(
        self, state: PlanAndExecuteState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Apply format code to the file changes made by the agent.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        await apply_format_code(config["configurable"]["source_repo_id"], config["configurable"]["source_ref"], store)
        return Command(goto=END)
