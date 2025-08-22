from __future__ import annotations

import logging
from typing import Literal

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import (
    Runnable,
    RunnableConfig,  # noqa: TC002
)
from langgraph.graph.state import END, CompiledStateGraph, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.nodes import apply_format_code
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.tools import think
from automation.tools.toolkits import FileNavigationToolkit

from .conf import settings
from .prompts import command_output_evaluator_human, pipeline_fixer_human, troubleshoot_human, troubleshoot_system
from .schemas import CommandOuputEvaluation, CommandOuputInput, TroubleshootingDetail
from .state import InputState, OutputState, OverallState, TroubleshootState
from .tools import complete_task

logger = logging.getLogger("daiv.agents")


MAX_FORMAT_ITERATIONS = 2


class CommandOutputEvaluator(BaseAgent[Runnable[CommandOuputInput, CommandOuputEvaluation]]):
    """
    Chain for evaluating the content of the output of a command to determine if there are any errors,
    or indications of failures.
    """

    async def compile(self) -> Runnable:
        return (
            ChatPromptTemplate.from_messages([command_output_evaluator_human])
            | BaseAgent.get_model(model=settings.COMMAND_OUTPUT_MODEL_NAME).with_structured_output(
                CommandOuputEvaluation, method="function_calling"
            )
        ).with_config({"run_name": "CommandOutputEvaluator"})


class PipelineFixerAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent for fixing pipeline failures.
    """

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the state graph for the agent.

        Returns:
            CompiledStateGraph: The compiled state graph.
        """
        workflow = StateGraph(OverallState, input_schema=InputState, output_schema=OutputState)

        workflow.add_node("troubleshoot", self.troubleshoot)
        workflow.add_node("plan_and_execute", self.plan_and_execute)
        workflow.add_node("apply_format_code", self.apply_format_code)

        workflow.set_entry_point("troubleshoot")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def troubleshoot(
        self, state: InputState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["plan_and_execute", "__end__"]]:
        """
        Troubleshoot the issue based on the logs from the failed CI/CD pipeline.

        This will determine whether the issue is directly related to the codebase or caused by external factors.

        Args:
            state (InputState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config to use for the agent.

        Returns:
            Command[Literal["plan_and_execute", "__end__"]]: The next step in the workflow.
        """
        tools = FileNavigationToolkit.get_tools() + [complete_task, think]

        agent = create_react_agent(
            model=BaseAgent.get_model(
                model=settings.TROUBLESHOOTING_MODEL_NAME, thinking_level=settings.TROUBLESHOOTING_THINKING_LEVEL
            ),
            tools=tools,
            state_schema=TroubleshootState,
            prompt=ChatPromptTemplate.from_messages([
                troubleshoot_system,
                troubleshoot_human,
                MessagesPlaceholder("messages"),
            ]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"),
                repo_id=config["configurable"]["source_repo_id"],
                job_name=config["configurable"]["job_name"],
            ),
            store=store,
            checkpointer=False,  # Disable checkpointer to avoid persisting the state in the store
            name="troubleshoot_react_agent",
            version="v2",
        )

        await agent.ainvoke({"job_logs": state["job_logs"], "diff": state["diff"], "messages": []})

        # At this stage, the agent has invoked the `complete_task` tool and next step is already set in the tool call.
        # If not, it means something went wrong and we need to fallback to a manual fix.
        return Command(
            goto=END,
            update={
                "need_manual_fix": True,
                "troubleshooting": state.get(
                    "troubleshooting",
                    [
                        TroubleshootingDetail(
                            title="Pipeline fix failed",
                            category="other",
                            details=(
                                "Couldn't fix the pipeline automatically due to an unexpected error. "
                                "Please review the logs and apply the necessary fixes manually."
                            ),
                        )
                    ],
                ),
            },
        )

    async def plan_and_execute(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Plan and execute the remediation steps to fix the identified codebase issues.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        plan_and_execute = await PlanAndExecuteAgent.get_runnable(store=store, checkpointer=self.checkpointer)

        await plan_and_execute.ainvoke({
            "messages": await pipeline_fixer_human.aformat_messages(
                troubleshooting_details=[
                    troubleshooting_detail
                    for troubleshooting_detail in state["troubleshooting"]
                    if troubleshooting_detail.category == "codebase"
                ]
            )
        })

        return Command(goto=END)

    async def apply_format_code(
        self, state: InputState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["plan_and_execute", "__end__"]]:
        """
        Apply format code to the repository to fix the linting issues in the pipeline.

        Args:
            state (InputState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["plan_and_execute", "__end__"]]: The next step in the workflow.
        """
        content = await apply_format_code(
            config["configurable"]["source_repo_id"], config["configurable"]["source_ref"], store
        )

        if content is None:
            # If format code is disabled, we need to try to fix the linting issues by planning the remediation steps.
            # This is less effective than actually formatting the code, but it's better than nothing. For instance,
            # linting errors like whitespaces can be challenging to fix by an agent, or even impossible.
            return Command(goto="plan_and_execute")

        # We need to check if the command output contains more errors, or indications of failures.
        # The command may not have been enough to fix the problems, so we need to check if there are any
        # errors left.
        command_output_evaluator = await CommandOutputEvaluator.get_runnable()
        result = await command_output_evaluator.ainvoke({"output": content})

        if result.has_errors and state.get("format_iteration", 0) < MAX_FORMAT_ITERATIONS:
            # If there are still errors, we need to try to fix them by executing troubleshooting again.
            return Command(
                goto="troubleshoot",
                update={"job_logs": content, "format_iteration": state.get("format_iteration", 0) + 1},
            )

        return Command(goto=END)
