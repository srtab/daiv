from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig  # noqa: TC002
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledGraph, CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.agents.prebuilt import prepare_repository_files_as_messages
from automation.agents.prompts import execute_plan_human, execute_plan_system
from automation.conf import settings
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import ReadRepositoryToolkit, SandboxToolkit, WebSearchToolkit, WriteRepositoryToolkit
from automation.utils import file_changes_namespace
from core.config import RepositoryConfig

from .prompts import plan_approval_system, plan_system
from .schemas import ExecuteState, HumanApproval, PlanAndExecuteConfig, PlanAndExecuteState
from .tools import determine_next_action

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import SystemMessage


logger = logging.getLogger("daiv.agents")

INTERRUPT_AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"


class PlanAndExecuteAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to plan and execute a task.
    """

    model_name = settings.CODING_PERFORMANT_MODEL_NAME
    fallback_model_name = settings.GENERIC_PERFORMANT_MODEL_NAME

    def __init__(self, *, human_in_the_loop: bool = True, store: BaseStore | None = None, **kwargs):
        """
        Initialize the agent.

        Args:
            human_in_the_loop (bool): Whether to include a human in the loop or execute the plan automatically.
            store (BaseStore): The store to use for caching.
        """
        self.store = store or InMemoryStore()
        self.human_in_the_loop = human_in_the_loop
        super().__init__(**kwargs)

    def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(PlanAndExecuteState, config_schema=PlanAndExecuteConfig)

        workflow.add_node("plan", self.plan_subgraph(self.store))
        workflow.add_node("plan_approval", self.plan_approval)
        workflow.add_node("execute_plan", self.execute)
        workflow.add_node("apply_lint_fix", self.apply_lint_fix)

        workflow.set_entry_point("plan")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store)

    def plan_subgraph(self, store: BaseStore) -> CompiledGraph:
        """
        Subgraph to plan the steps to follow.

        Args:
            store (BaseStore): The store to use for caching.

        Returns:
            CompiledGraph: The compiled subgraph.
        """

        tools = (
            ReadRepositoryToolkit.create_instance().get_tools()
            + WebSearchToolkit.create_instance().get_tools()
            + SandboxToolkit.create_instance().get_tools()
        )

        system_message = cast(
            "SystemMessage",
            plan_system.format(tools=[tool.name for tool in tools], recursion_limit=settings.RECURSION_LIMIT),
        )

        return create_react_agent(
            self.model.with_fallbacks([cast("BaseChatModel", self.fallback_model)]),
            tools=tools + [determine_next_action],
            store=store,
            prompt=system_message,
        )

    def plan_approval(self, state: PlanAndExecuteState) -> Command[Literal["execute_plan", "plan_approval"]]:
        """
        Request human approval of the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.

        Returns:
            Command[Literal["execute_plan", "plan_approval"]]: The next step in the workflow.
        """
        if not self.human_in_the_loop:
            return Command(goto="execute_plan")

        messages = interrupt(INTERRUPT_AWAITING_PLAN_APPROVAL)

        plan_approval_evaluator = self.get_model(
            model=settings.GENERIC_COST_EFFICIENT_MODEL_NAME
        ).with_structured_output(HumanApproval)

        result = cast("HumanApproval", plan_approval_evaluator.invoke([plan_approval_system] + messages))

        if result.is_unambiguous_approval:
            return Command(goto="execute_plan", update={"plan_approval_response": result.feedback})
        return Command(goto="plan_approval", update={"plan_approval_response": result.feedback})

    def execute(
        self, state: PlanAndExecuteState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["apply_lint_fix", "__end__"]]:
        """
        Execute the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["apply_lint_fix", "__end__"]]: The next step in the workflow.
        """
        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        tools = WriteRepositoryToolkit.create_instance().get_tools() + SandboxToolkit.create_instance().get_tools()

        react_agent = create_react_agent(
            self.model.with_fallbacks([cast("BaseChatModel", self.fallback_model)]),
            state_schema=ExecuteState,
            tools=tools,
            store=store,
            prompt=execute_plan_system,
        )

        messages = ChatPromptTemplate.from_messages([execute_plan_human]).format_messages(
            plan_goal=state["plan_goal"], plan_tasks=enumerate(state["plan_tasks"])
        ) + prepare_repository_files_as_messages(
            source_repo_id, source_ref, [task.path for task in state["plan_tasks"]], store
        )

        react_agent.invoke({"messages": messages}, config={"recursion_limit": settings.RECURSION_LIMIT})

        if store.search(file_changes_namespace(source_repo_id, source_ref), limit=1):
            return Command(goto="apply_lint_fix")
        return Command(goto=END)

    def apply_lint_fix(
        self, state: PlanAndExecuteState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Apply lint fix to the file changes made by the agent.

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
            logger.info("Lint fix is disabled for this repository, skipping.")
            return Command(goto=END)

        run_command_tool = RunSandboxCommandsTool()
        run_command_tool.invoke(
            {
                "commands": [repo_config.commands.install_dependencies, repo_config.commands.format_code],
                "intent": "Fix linting issues",
                "store": store,
            },
            config={"configurable": {"source_repo_id": source_repo_id, "source_ref": source_ref}},
        )

        return Command(goto=END)
