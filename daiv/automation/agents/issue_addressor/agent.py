from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig  # noqa: TC002
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.image_url_extractor.agent import ImageURLExtractorAgent
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from core.config import RepositoryConfig

from .conf import settings
from .prompts import issue_addressor_human, issue_evaluator_human, issue_evaluator_system
from .schemas import IssueAssessmentEvaluation, IssueAssessmentInput
from .state import OverallState

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver


logger = logging.getLogger("daiv.agents")


class IssueEvaluator(BaseAgent[Runnable[IssueAssessmentInput, IssueAssessmentEvaluation]]):
    """
    Agent to evaluate the issue is a request for changes to the codebase.
    """

    async def compile(self) -> Runnable:
        return (
            ChatPromptTemplate.from_messages([issue_evaluator_system, issue_evaluator_human])
            | BaseAgent.get_model(model=settings.ISSUE_EVALUATOR_MODEL_NAME).with_structured_output(
                IssueAssessmentEvaluation, method="function_calling"
            )
        ).with_config({"run_name": "IssueEvaluator"})


class IssueAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address issues created by the reporter.
    """

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(OverallState)

        workflow.add_node("assessment", self.assessment)
        workflow.add_node("prepare_data", self.prepare_data)
        workflow.add_node("plan_and_execute", await self.plan_and_execute_subgraph(self.checkpointer, self.store))

        workflow.set_entry_point("assessment")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def assessment(self, state: OverallState) -> Command[Literal["prepare_data", "__end__"]]:
        """
        Assess the issue created by the reporter.

        This node will help distinguish if the issue is a request to change something on the codebase or not.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Command[Literal["prepare_data", "__end__"]]: The next step in the workflow.
        """
        evaluator = await IssueEvaluator.get_runnable()
        response = await evaluator.ainvoke({
            "issue_title": state["issue_title"],
            "issue_description": state["issue_description"],
        })

        if response.request_for_changes:
            return Command[Literal["prepare_data"]](goto="prepare_data")
        # TODO: ask for clarification if the issue is not a request for changes
        return Command[Literal["__end__"]](goto="__end__", update={"request_for_changes": False})

    async def prepare_data(self, state: OverallState, config: RunnableConfig) -> Command[Literal["plan_and_execute"]]:
        """
        Prepare the data for the plan and execute node.
        This node will extract the images from the issue description.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Command[Literal["plan_and_execute"]]: The next step in the workflow.
        """
        repo_config = RepositoryConfig.get_config(config["configurable"]["source_repo_id"])

        extractor = await ImageURLExtractorAgent.get_runnable()
        extracted_images = await extractor.ainvoke(
            {"markdown_text": state["issue_description"]},
            {
                "configurable": {
                    "repo_client_slug": config["configurable"].get("repo_client"),
                    "project_id": config["configurable"].get("project_id"),
                }
            },
        )

        return Command[Literal["plan_and_execute"]](
            goto="plan_and_execute",
            update={
                "messages": await HumanMessagePromptTemplate.from_template(
                    [issue_addressor_human] + extracted_images, "jinja2"
                ).aformat_messages(
                    issue_title=state["issue_title"],
                    issue_description=state["issue_description"],
                    project_description=repo_config.repository_description,
                )
            },
        )

    async def plan_and_execute_subgraph(
        self, checkpointer: BaseCheckpointSaver | None, store: BaseStore | None
    ) -> CompiledStateGraph:
        """
        Compile the subgraph for the plan and execute node that will be used to address the issue.

        Args:
            store (BaseStore): The store to persist file changes.

        Returns:
            CompiledStateGraph: The compiled subgraph.
        """
        return await PlanAndExecuteAgent.get_runnable(checkpointer=checkpointer, store=store)
