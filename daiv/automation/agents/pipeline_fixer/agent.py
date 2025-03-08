from __future__ import annotations

import logging
from typing import Literal, cast

from langchain_core.output_parsers.openai_tools import PydanticToolsParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig  # noqa: TC002
from langgraph.graph.state import END, START, CompiledStateGraph, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.agents.plan_and_execute.schemas import Plan, Task
from automation.conf import settings
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import ReadRepositoryToolkit
from core.config import RepositoryConfig

from .prompts import error_log_evaluator_human, error_log_evaluator_system, troubleshoot_human, troubleshoot_system
from .schemas import ErrorLogEvaluation, TroubleshootingDetail
from .state import OverallState, TroubleshootState
from .tools import troubleshoot_analysis_result

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

    def troubleshoot(
        self, state: OverallState, store: BaseStore
    ) -> Command[Literal["apply_unittest_fix", "apply_lint_fix", "__end__"]]:
        """
        Troubleshoot the issue based on the logs from the failed CI/CD pipeline.

        This will determine whether the issue is directly related to the codebase or caused by external factors.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Command[Literal["apply_unittest_fix", "apply_lint_fix", "__end__"]]: The next step in the workflow.
        """
        tools = ReadRepositoryToolkit.create_instance().get_tools()

        agent = create_react_agent(
            model=self.get_model(
                model=settings.PIPELINE_FIXER.MODEL_NAME, thinking_level=settings.PIPELINE_FIXER.THINKING_LEVEL
            ),
            tools=tools + [troubleshoot_analysis_result],
            state_schema=TroubleshootState,
            prompt=ChatPromptTemplate.from_messages([
                troubleshoot_system,
                troubleshoot_human,
                MessagesPlaceholder("messages"),
            ]),
            store=store,
            checkpointer=False,  # Disable checkpointer to avoid persisting the state in the store
            name="troubleshoot_react_agent",
            version="v2",
        )

        agent.invoke({"job_logs": state["job_logs"], "diff": state["diff"], "messages": []})

        # At this stage, the agent has invoked the troubleshoot_analysis_result tool and next step is
        # already set in the tool call. If not, it means something went wrong and we need to fallback to a
        # manual fix.
        return Command(
            goto=END,
            update={
                "troubleshooting": [
                    TroubleshootingDetail(
                        details="Couldn't fix the pipeline automatically.",
                        remediation_steps=["Please review the logs and apply the necessary fixes manually."],
                    )
                ],
                "need_manual_fix": True,
            },
        )

    def apply_unittest_fix(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Apply the unittest fix using the plan and execute agent.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
            config (RunnableConfig): The config to use for the agent.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        plan = Plan(
            goal="Fix the identified issues in the unittest pipeline to make it pass.",
            tasks=[
                Task(
                    title=troubleshooting.title,
                    description=troubleshooting.details,
                    path=troubleshooting.file_path,
                    context_paths=[troubleshooting.file_path],
                    subtasks=troubleshooting.remediation_steps,
                )
                for troubleshooting in state["troubleshooting"]
            ],
        )

        plan_and_execute = PlanAndExecuteAgent(store=store, skip_planning=True, skip_approval=True, checkpointer=False)
        plan_and_execute.agent.invoke({"plan_goal": plan.goal, "plan_tasks": plan.tasks})

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
