from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, MessagesPlaceholder
from langchain_core.runnables import (
    Runnable,
    RunnableConfig,  # noqa: TC002
)
from langchain_core.runnables.config import DEFAULT_RECURSION_LIMIT
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledGraph, CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import MCPToolkit, ReadRepositoryToolkit, WebSearchToolkit, WriteRepositoryToolkit
from automation.utils import file_changes_namespace
from core.config import RepositoryConfig

from .conf import settings
from .prompts import execute_plan_human, execute_plan_system, human_approval_system, plan_system
from .schemas import HumanApprovalEvaluation, HumanApprovalInput
from .state import ExecuteState, PlanAndExecuteConfig, PlanAndExecuteState
from .tools import determine_next_action, think_plan, think_plan_executer

if TYPE_CHECKING:
    from langchain_core.prompts import SystemMessagePromptTemplate

logger = logging.getLogger("daiv.agents")


class HumanApprovalEvaluator(BaseAgent[Runnable[HumanApprovalInput, HumanApprovalEvaluation]]):
    """
    Chain for evaluating the human approval of the plan.
    """

    async def compile(self) -> Runnable:
        return (
            ChatPromptTemplate.from_messages([human_approval_system, MessagesPlaceholder("messages")])
            | self.get_model(model=settings.HUMAN_APPROVAL_MODEL_NAME).with_structured_output(
                HumanApprovalEvaluation, method="function_calling"
            )
        ).with_config({"run_name": "HumanApprovalEvaluator"})


class PlanAndExecuteAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to plan and execute a task.
    """

    def __init__(
        self,
        *,
        skip_planning: bool = False,
        skip_approval: bool = False,
        plan_system_template: SystemMessagePromptTemplate | None = None,
        **kwargs,
    ):
        """
        Initialize the agent.

        Args:
            skip_planning (bool): Whether to skip the planning step.
            skip_approval (bool): Whether to skip the approval step.
            plan_system_template (SystemMessagePromptTemplate): The system prompt template for the planning step.
        """
        self.plan_system_template = plan_system_template or plan_system
        self.skip_planning = skip_planning
        self.skip_approval = skip_approval
        super().__init__(**kwargs)

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(PlanAndExecuteState, config_schema=PlanAndExecuteConfig)

        if not self.skip_planning:
            workflow.add_node("plan", await self.plan_subgraph(self.store))
            workflow.add_node("plan_approval", self.plan_approval)

        workflow.add_node("execute_plan", self.execute_plan)
        workflow.add_node("apply_format_code", self.apply_format_code)

        if not self.skip_planning:
            workflow.set_entry_point("plan")
        else:
            workflow.set_entry_point("execute_plan")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def plan_subgraph(self, store: BaseStore | None = None) -> CompiledGraph:
        """
        Subgraph to plan the steps to follow.

        Args:
            store (BaseStore): The store to use for caching.

        Returns:
            CompiledGraph: The compiled subgraph.
        """

        return create_react_agent(
            self.get_model(model=settings.PLANNING_MODEL_NAME),
            tools=ReadRepositoryToolkit.create_instance().get_tools()
            + WebSearchToolkit.create_instance().get_tools()
            + (await MCPToolkit.create_instance()).get_tools()
            + [think_plan, determine_next_action],
            store=store,
            checkpointer=False,  # Disable checkpointer to avoid storing the plan in the store
            prompt=ChatPromptTemplate.from_messages([
                self.plan_system_template,
                MessagesPlaceholder("messages"),
            ]).partial(current_date_time=timezone.now().strftime("%d %B, %Y %H:%M")),
            name="Planner",
            version="v2",
        )

    async def plan_approval(self, state: PlanAndExecuteState) -> Command[Literal["execute_plan", "plan_approval"]]:
        """
        Request human approval of the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.

        Returns:
            Command[Literal["execute_plan", "plan_approval"]]: The next step in the workflow.
        """
        if self.skip_approval:
            return Command(goto="execute_plan")

        messages = interrupt({"plan_tasks": state.get("plan_tasks"), "plan_questions": state.get("plan_questions")})

        human_approval_evaluator = await HumanApprovalEvaluator().agent
        result = await human_approval_evaluator.ainvoke({"messages": messages})

        if result.is_unambiguous_approval:
            return Command(goto="execute_plan", update={"plan_approval_response": result.feedback})
        return Command(goto="plan_approval", update={"plan_approval_response": result.feedback})

    async def execute_plan(
        self, state: PlanAndExecuteState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["apply_format_code", "__end__"]]:
        """
        Subgraph to execute the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["apply_format_code", "__end__"]]: The next step in the workflow.
        """
        react_agent = create_react_agent(
            self.get_model(model=settings.EXECUTION_MODEL_NAME),
            state_schema=ExecuteState,
            tools=WriteRepositoryToolkit.create_instance().get_tools() + [think_plan_executer],
            store=store,
            prompt=ChatPromptTemplate.from_messages([
                execute_plan_system,
                HumanMessagePromptTemplate.from_template(execute_plan_human, "jinja2"),
                MessagesPlaceholder("messages"),
            ]).partial(current_date_time=timezone.now().strftime("%d %B, %Y %H:%M")),
            checkpointer=False,  # Disable checkpointer to avoid storing the execution in the store
            name="PlanExecuter",
            version="v2",
        )

        await react_agent.ainvoke(
            {
                "plan_tasks": state["plan_tasks"],
                "relevant_files": list({
                    file_path for task in state["plan_tasks"] for file_path in task.relevant_files
                }),
            },
            config=RunnableConfig(recursion_limit=config.get("recursion_limit", DEFAULT_RECURSION_LIMIT)),
        )

        if await store.asearch(
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
        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        repo_config = RepositoryConfig.get_config(source_repo_id)

        if not repo_config.commands.enabled():
            logger.info("Format code is disabled for this repository, skipping.")
            return Command(goto=END)

        run_command_tool = RunSandboxCommandsTool()
        await run_command_tool.ainvoke(
            {
                "commands": [repo_config.commands.install_dependencies, repo_config.commands.format_code],
                "intent": "[Manual call] Format code in the repository",
                "store": store,
            },
            config=RunnableConfig(configurable={"source_repo_id": source_repo_id, "source_ref": source_ref}),
        )

        return Command(goto=END)
