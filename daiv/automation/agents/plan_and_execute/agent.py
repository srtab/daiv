from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder, SystemMessagePromptTemplate
from langchain_core.runnables import (
    RunnableConfig,  # noqa: TC002
    RunnableLambda,
)
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledGraph, CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command, interrupt

from automation.agents import BaseAgent
from automation.conf import settings
from automation.tools.repository import RetrieveFileContentTool
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import ReadRepositoryToolkit, SandboxToolkit, WebSearchToolkit, WriteRepositoryToolkit
from automation.utils import file_changes_namespace
from core.config import RepositoryConfig

from .prompts import execute_plan_human, execute_plan_system, plan_approval_system, plan_system
from .schemas import HumanApproval, Task
from .state import ExecuteState, PlanAndExecuteConfig, PlanAndExecuteState
from .tools import determine_next_action

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import BaseMessage, SystemMessage
    from langgraph.prebuilt.chat_agent_executor import AgentState


logger = logging.getLogger("daiv.agents")

INTERRUPT_AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"


def prompt_system(tools_names: list[str], current_date_time: str) -> RunnableLambda:
    def prompt(state: AgentState, config: RunnableConfig) -> Sequence[BaseMessage]:
        system_message = cast(
            "SystemMessage",
            plan_system.format(
                tools=tools_names,
                recursion_limit=config.get("recursion_limit", settings.RECURSION_LIMIT),
                current_date_time=current_date_time,
            ),
        )
        return [system_message] + state["messages"]

    return RunnableLambda(prompt)


class PlanAndExecuteAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to plan and execute a task.
    """

    def __init__(
        self,
        *,
        plan_system_template: SystemMessagePromptTemplate | None = None,
        human_in_the_loop: bool = True,
        **kwargs,
    ):
        """
        Initialize the agent.

        Args:
            human_in_the_loop (bool): Whether to include a human in the loop or execute the plan automatically.
        """
        self.human_in_the_loop = human_in_the_loop
        self.plan_system_template = plan_system_template or plan_system
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
        workflow.add_node("execute_plan", self.execute_plan)
        workflow.add_node("apply_format_code", self.apply_format_code)

        workflow.set_entry_point("plan")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store)

    def plan_subgraph(self, store: BaseStore | None = None) -> CompiledGraph:
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

        tools_names = [tool.name for tool in tools]

        def prompt(state: AgentState, config: RunnableConfig) -> Sequence[BaseMessage]:
            system_message = cast(
                "SystemMessage",
                self.plan_system_template.format(
                    tools=tools_names, recursion_limit=config.get("recursion_limit", settings.RECURSION_LIMIT)
                ),
            )
            return [system_message] + state["messages"]

        return create_react_agent(
            # FIXME: Add fallback to generic performant model, now it's not possible because do to imcompatibility
            # between reasoning models and non-reasoning models. Both models need to be reasoning models.
            self.get_model(
                model=settings.PLAN_AND_EXECUTE.PLANNING_MODEL_NAME,
                thinking_level=settings.PLAN_AND_EXECUTE.PLANNING_THINKING_LEVEL,
            ),
            tools=tools + [determine_next_action],
            store=store,
            checkpointer=False,  # Disable checkpointer to avoid storing the plan in the store
            prompt=RunnableLambda(prompt),
            name="plan_react_agent",
            version="v2",
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
            model=settings.PLAN_AND_EXECUTE.PLAN_APPROVAL_MODEL_NAME
        ).with_structured_output(HumanApproval)

        result = cast("HumanApproval", plan_approval_evaluator.invoke([plan_approval_system] + messages))

        if result.is_unambiguous_approval:
            return Command(goto="execute_plan", update={"plan_approval_response": result.feedback})
        return Command(goto="plan_approval", update={"plan_approval_response": result.feedback})

    def execute_plan(
        self, state: PlanAndExecuteState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["apply_format_code", "__end__"]]:
        """
        Execute the plan.

        Args:
            state (PlanAndExecuteState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["apply_format_code", "__end__"]]: The next step in the workflow.
        """
        source_repo_id = config["configurable"]["source_repo_id"]
        source_ref = config["configurable"]["source_ref"]

        react_agent = create_react_agent(
            # FIXME: Add fallback to generic performant model, now it's not possible because do to imcompatibility
            # between reasoning models and non-reasoning models. Both models need to be reasoning models.
            self.get_model(
                model=settings.PLAN_AND_EXECUTE.EXECUTION_MODEL_NAME,
                thinking_level=settings.PLAN_AND_EXECUTE.EXECUTION_THINKING_LEVEL,
            ),
            state_schema=ExecuteState,
            tools=WriteRepositoryToolkit.create_instance().get_tools() + SandboxToolkit.create_instance().get_tools(),
            store=store,
            prompt=ChatPromptTemplate.from_messages([
                execute_plan_system,
                execute_plan_human,
                MessagesPlaceholder("messages"),
            ]),
            checkpointer=False,  # Disable checkpointer to avoid storing the execution in the store
            name="execute_react_agent",
            version="v2",
        )

        react_agent.invoke(
            {
                "plan_goal": state["plan_goal"],
                "plan_tasks": list(enumerate(state["plan_tasks"])),
                "file_contents": self._prepare_repository_files(source_repo_id, source_ref, state["plan_tasks"], store),
            },
            config={"recursion_limit": config.get("recursion_limit", settings.RECURSION_LIMIT)},
        )

        if store.search(file_changes_namespace(source_repo_id, source_ref), limit=1):
            return Command(goto="apply_format_code")
        return Command(goto=END)

    def apply_format_code(
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
        run_command_tool.invoke(
            {
                "commands": [repo_config.commands.install_dependencies, repo_config.commands.format_code],
                "intent": "[Manual call] Format code in the repository",
                "store": store,
            },
            config={"configurable": {"source_repo_id": source_repo_id, "source_ref": source_ref}},
        )

        return Command(goto=END)

    def _prepare_repository_files(
        self, source_repo_id: str, source_ref: str, plan_tasks: list[Task], store: BaseStore
    ) -> list[str]:
        """
        Prepare the repository files to be included in the context of the agent.

        Args:
            source_repo_id (str): The ID of the source repository.
            source_ref (str): The reference of the source repository.
            plan_tasks (list[Task]): The tasks of the plan.
            store (BaseStore): The store to use for caching.

        Returns:
            list[str]: The repository files to be included in the context of the agent.
        """
        retrieve_file_content_tool = RetrieveFileContentTool(return_not_found_message=False)
        paths = set()
        for task in plan_tasks:
            paths.update(task.context_paths)
            paths.add(task.path)

        file_contents = []

        for path in paths:
            if repository_file_content := retrieve_file_content_tool.invoke(
                {"file_path": path, "intent": "[Manual call] Check current implementation", "store": store},
                config={"configurable": {"source_repo_id": source_repo_id, "source_ref": source_ref}},
            ):
                file_contents.append(repository_file_content)

        if not file_contents:
            return []

        return file_contents
