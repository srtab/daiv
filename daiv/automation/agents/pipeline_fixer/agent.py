from __future__ import annotations

import logging
from typing import Literal, cast

from langchain_core.output_parsers.openai_tools import PydanticToolsParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig  # noqa: TC002
from langgraph.graph.state import END, START, CompiledStateGraph, StateGraph
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.conf import settings
from automation.tools.sandbox import RunSandboxCommandsTool
from core.config import RepositoryConfig

from .prompts import (
    autofix_human,
    error_log_evaluator_human,
    error_log_evaluator_system,
    troubleshoot_human,
    troubleshoot_system,
)
from .schemas import ErrorLogEvaluation, PipelineLogClassification, TroubleshootingDetail
from .state import OverallState

logger = logging.getLogger("daiv.agents")


class PipelineFixerAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent for fixing pipeline failures.
    """

    def compile(self) -> CompiledStateGraph:
        """
        Compile the state graph for the agent.

        Returns:
            CompiledStateGraph: The compiled state graph.
        """
        workflow = StateGraph(OverallState)

        workflow.add_node("should_try_to_fix", self.should_try_to_fix)
        workflow.add_node("troubleshoot", self.troubleshoot)
        workflow.add_node("apply_unittest_fix", self.apply_unittest_fix)
        workflow.add_node("apply_lint_fix", self.apply_lint_fix)

        workflow.add_edge(START, "should_try_to_fix")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store)

    def should_try_to_fix(
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
        if state.get("iteration", 0) >= settings.PIPELINE_FIXER_MAX_RETRY:
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
                            details="Maximum retry iterations reached for automatic pipeline fix.",
                            remediation_steps=["Please review the logs and apply the necessary fixes manually."],
                        )
                    ],
                },
            )

        if state.get("previous_job_logs") is None:
            # This means that it's the first time the agent is running, so we need to troubleshoot the issue.
            return Command(goto="troubleshoot", update={"iteration": state.get("iteration", 0) + 1})

        evaluator = (
            ChatPromptTemplate.from_messages([error_log_evaluator_system, error_log_evaluator_human])
            | self.get_model(model=settings.CODING_COST_EFFICIENT_MODEL_NAME)
            .bind_tools([ErrorLogEvaluation], tool_choice="auto")
            .with_fallbacks([
                self.get_model(model=settings.CODING_PERFORMANT_MODEL_NAME).bind_tools(
                    [ErrorLogEvaluation], tool_choice="auto"
                )
            ])
            | PydanticToolsParser(tools=[ErrorLogEvaluation], first_tool_only=True)
        )

        result = cast(
            "ErrorLogEvaluation",
            evaluator.invoke({
                "log_trace_1": cast("str", state["previous_job_logs"]),
                "log_trace_2": state["job_logs"],
            }),
        )

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
                            details="Couldn't fix the pipeline automatically.",
                            remediation_steps=["Please review the logs and apply the necessary fixes manually."],
                        )
                    ],
                },
            )

        return Command(goto="troubleshoot", update={"iteration": state.get("iteration", 0) + 1})

    def troubleshoot(self, state: OverallState) -> Command[Literal["apply_unittest_fix", "apply_lint_fix", "__end__"]]:
        """
        Troubleshoot the issue based on the logs from the failed CI/CD pipeline.

        This will determine whether the issue is directly related to the codebase or caused by external factors.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Command[Literal["apply_unittest_fix", "apply_lint_fix", "__end__"]]: The next step in the workflow.
        """
        evaluator = (
            ChatPromptTemplate.from_messages([troubleshoot_system, troubleshoot_human])
            | self.get_model(model=settings.CODING_COST_EFFICIENT_MODEL_NAME)
            .bind_tools([PipelineLogClassification], tool_choice="auto")
            .with_fallbacks([
                self.get_model(model=settings.CODING_PERFORMANT_MODEL_NAME).bind_tools(
                    [PipelineLogClassification], tool_choice="auto"
                )
            ])
            | PydanticToolsParser(tools=[PipelineLogClassification], first_tool_only=True)
        )

        response = cast(
            "PipelineLogClassification", evaluator.invoke({"job_logs": state["job_logs"], "diff": state["diff"]})
        )

        if response.category == "codebase":
            if response.pipeline_phase == "lint":
                return Command(goto="apply_lint_fix", update={"troubleshooting": response.troubleshooting})

            elif response.pipeline_phase == "unittest":
                return Command(goto="apply_unittest_fix", update={"troubleshooting": response.troubleshooting})

        return Command(goto=END, update={"troubleshooting": response.troubleshooting, "need_manual_fix": True})

    def apply_unittest_fix(self, state: OverallState, store: BaseStore, config: RunnableConfig):
        """
        Apply the unittest fix using the plan and execute agent.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config to use for the agent.

        Returns:
            Command[Literal["apply_lint_fix", END]]: The next step in the workflow.
        """
        message = autofix_human.format(job_logs=state["job_logs"], troubleshooting_details=state["troubleshooting"])

        plan_and_execute = PlanAndExecuteAgent(store=store, human_in_the_loop=False, checkpointer=False)

        result = plan_and_execute.agent.invoke({"messages": [message]})

        if result.get("plan_questions"):
            return Command(
                goto=END,
                update={
                    "need_manual_fix": True,
                    "troubleshooting": [
                        TroubleshootingDetail(
                            details="Couldn't fix the pipeline automatically.",
                            remediation_steps=["Please review the logs and apply the necessary fixes manually."],
                        )
                    ],
                },
            )
        return Command(goto=END)

    def apply_lint_fix(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Apply the lint fix.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config to use for the agent.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        repo_config = RepositoryConfig.get_config(config["configurable"]["source_repo_id"])

        if not repo_config.commands.enabled():
            logger.info("Format code is disabled for this repository, skipping.")
            return Command(
                goto=END,
                update={
                    "need_manual_fix": True,
                    "troubleshooting": [
                        TroubleshootingDetail(
                            details="Format code is disabled for this repository.",
                            remediation_steps=[
                                "Consider enabling format code in the repository configuration.",
                                "Please review the logs and apply the necessary fixes manually.",
                            ],
                        )
                    ],
                },
            )

        run_command_tool = RunSandboxCommandsTool()
        run_command_tool.invoke(
            {
                "commands": [repo_config.commands.install_dependencies, repo_config.commands.format_code],
                "intent": "[Manual run] Format code in the repository",
                "store": store,
            },
            config={
                "configurable": {
                    "source_repo_id": config["configurable"]["source_repo_id"],
                    "source_ref": config["configurable"]["source_ref"],
                }
            },
        )

        return Command(goto=END)
