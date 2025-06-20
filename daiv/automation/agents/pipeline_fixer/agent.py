from __future__ import annotations

import logging
import uuid
from typing import Literal, cast

from django.utils import timezone

from langchain_core.output_parsers.openai_tools import PydanticToolsParser
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
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.agents.plan_and_execute.schemas import ChangeInstructions, Plan
from automation.tools import think
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import ReadRepositoryToolkit
from core.config import RepositoryConfig

from .conf import settings
from .prompts import (
    command_output_evaluator_human,
    same_error_evaluator_human,
    same_error_evaluator_system,
    troubleshoot_human,
    troubleshoot_system,
)
from .schemas import (
    CommandOuputEvaluation,
    CommandOuputInput,
    SameErrorEvaluation,
    SameErrorInput,
    TroubleshootingDetail,
)
from .state import OverallState, TroubleshootState
from .tools import complete_task

logger = logging.getLogger("daiv.agents")


class SameErrorEvaluator(BaseAgent[Runnable[SameErrorInput, SameErrorEvaluation]]):
    """
    Chain for evaluating if two logs are the same error.
    """

    async def compile(self) -> Runnable:
        return (
            ChatPromptTemplate.from_messages([same_error_evaluator_system, same_error_evaluator_human])
            | self.get_model(model=settings.SAME_ERROR_MODEL_NAME).bind_tools([SameErrorEvaluation], tool_choice="auto")
            | PydanticToolsParser(tools=[SameErrorEvaluation], first_tool_only=True)
        ).with_config({"run_name": "SameErrorEvaluator"})


class CommandOutputEvaluator(BaseAgent[Runnable[CommandOuputInput, CommandOuputEvaluation]]):
    """
    Chain for evaluating the content of the output of a command to determine if there are any errors,
    or indications of failures.
    """

    async def compile(self) -> Runnable:
        return (
            ChatPromptTemplate.from_messages([command_output_evaluator_human])
            | self.get_model(model=settings.COMMAND_OUTPUT_MODEL_NAME).with_structured_output(
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
        workflow = StateGraph(OverallState)

        workflow.add_node("should_try_to_fix", self.should_try_to_fix)
        workflow.add_node("troubleshoot", self.troubleshoot)
        workflow.add_node("execute_remediation_steps", self.execute_remediation_steps)
        workflow.add_node("apply_format_code", self.apply_format_code)

        workflow.set_entry_point("should_try_to_fix")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def should_try_to_fix(
        self, state: OverallState, config: RunnableConfig
    ) -> Command[Literal["troubleshoot", "__end__"]]:
        """
        Determine if the agent should try to fix the pipeline issue.

        If the agent has reached the maximum number of retries, or if the previous job logs are not available,
        the agent will try to fix the pipeline issue.

        If the previous job logs are available, the agent will invoke the error log evaluator to determine if the
        error is the same as the previous one. This is important to prevent the agent from applying the same fix
        over and over.

        Args:
            state (OverallState): The state of the agent.
            config (RunnableConfig): The config to use for the agent.

        Returns:
            Command[Literal["troubleshoot", "__end__"]]: The next step in the workflow.
        """
        if state.get("iteration", 0) >= settings.MAX_ITERATIONS:
            logger.warning(
                "Max retry iterations reached for pipeline fix on %s[%s] for job %s",
                config["configurable"]["source_repo_id"],
                config["configurable"]["source_ref"],
                config["configurable"]["job_name"],
            )
            return Command(
                goto=END,
                update={
                    "need_manual_fix": True,
                    "troubleshooting": [
                        TroubleshootingDetail(
                            title="Automatic fix failed",
                            details=(
                                "Maximum retry iterations reached for automatic pipeline fix. "
                                "Please review the logs and apply the necessary fixes manually."
                            ),
                        )
                    ],
                },
            )

        if state.get("previous_job_logs") is None:
            # This means that it's the first time the agent is running, so we need to troubleshoot the issue.
            return Command(goto="troubleshoot", update={"iteration": state.get("iteration", 0) + 1})

        same_error_evaluator = await SameErrorEvaluator().agent
        result = await same_error_evaluator.ainvoke({
            "log_trace_1": cast("str", state["previous_job_logs"]),
            "log_trace_2": state["job_logs"],
        })

        if result.is_same_error:
            logger.warning(
                "Not applying pipeline fix on %s[%s] for job %s because it's the same error as the previous one",
                config["configurable"]["source_repo_id"],
                config["configurable"]["source_ref"],
                config["configurable"]["job_name"],
            )
            return Command(
                goto=END,
                update={
                    "need_manual_fix": True,
                    "troubleshooting": [
                        TroubleshootingDetail(
                            title="Automatic fix skipped",
                            details=(
                                "Automatic fix skipped because the error is the same as the previous one. "
                                "Please review the logs and apply the necessary fixes manually."
                            ),
                        )
                    ],
                },
            )

        return Command(goto="troubleshoot", update={"iteration": state.get("iteration", 0) + 1})

    async def troubleshoot(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["execute_remediation_steps", "apply_format_code", "__end__"]]:
        """
        Troubleshoot the issue based on the logs from the failed CI/CD pipeline.

        This will determine whether the issue is directly related to the codebase or caused by external factors.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config to use for the agent.

        Returns:
            Command[Literal["execute_remediation_steps", "apply_format_code", "__end__"]]: The next step in
                the workflow.
        """
        tools = ReadRepositoryToolkit.create_instance().get_tools()

        agent = create_react_agent(
            model=self.get_model(
                model=settings.TROUBLESHOOTING_MODEL_NAME, thinking_level=settings.TROUBLESHOOTING_THINKING_LEVEL
            ),
            tools=tools + [complete_task, think],
            state_schema=TroubleshootState,
            prompt=ChatPromptTemplate.from_messages([
                troubleshoot_system,
                troubleshoot_human,
                MessagesPlaceholder("messages"),
            ]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y %H:%M"),
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
                "troubleshooting": [
                    TroubleshootingDetail(
                        title="Pipeline fix failed",
                        details=(
                            "Couldn't fix the pipeline automatically due to an unexpected error. "
                            "Please review the logs and apply the necessary fixes manually."
                        ),
                    )
                ]
                + state.get("troubleshooting", []),
            },
        )

    async def execute_remediation_steps(self, state: OverallState, store: BaseStore) -> Command[Literal["__end__"]]:
        """
        Execute the remediation steps to fix the pipeline issue.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        plan = Plan(
            changes=[
                ChangeInstructions(
                    file_path=troubleshooting.file_path,
                    details="\n".join(troubleshooting.remediation_steps),
                    relevant_files=[troubleshooting.file_path],
                )
                for troubleshooting in state["troubleshooting"]
            ]
        )

        plan_and_execute = await PlanAndExecuteAgent(
            store=store, skip_planning=True, skip_approval=True, checkpointer=False
        ).agent
        await plan_and_execute.ainvoke({"plan_tasks": plan.changes})

        return Command(goto=END)

    async def apply_format_code(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["execute_remediation_steps", "__end__"]]:
        """
        Apply format code to the repository to fix the linting issues in the pipeline.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["execute_remediation_steps", "__end__"]]: The next step in the workflow.
        """
        repo_config = RepositoryConfig.get_config(config["configurable"]["source_repo_id"])

        if not repo_config.commands.enabled():
            logger.info("Format code is disabled for this repository, skipping.")
            # If format code is disabled, we need to try to fix the linting issues by executing the remediation steps.
            # This is less effective than actually formatting the code, but it's better than nothing. For instance,
            # linting errors like whitespaces can be challenging to fix by an agent, or even impossible.
            return Command(goto="execute_remediation_steps")

        tool_message = await RunSandboxCommandsTool().ainvoke(
            {
                "name": "run_sandbox_commands",
                "args": {
                    "commands": [repo_config.commands.install_dependencies, repo_config.commands.format_code],
                    "intent": "[Manual run] Format code in the repository to fix the pipeline issue.",
                    "store": store,
                },
                "id": str(uuid.uuid4()),
                "type": "tool_call",
            },
            config=RunnableConfig(
                configurable={
                    "source_repo_id": config["configurable"]["source_repo_id"],
                    "source_ref": config["configurable"]["source_ref"],
                }
            ),
        )

        # We need to check if the command output contains more errors, or indications of failures.
        # The command may not have been enough to fix the problems, so we need to check if there are any
        # errors left.
        command_output_evaluator = await CommandOutputEvaluator().agent
        result = await command_output_evaluator.ainvoke({"output": tool_message.artifact[-1].output})

        if result.has_errors:
            # If there are still errors, we need to try to fix them by executing the remediation steps.
            return Command(goto="execute_remediation_steps")

        return Command(goto=END)
