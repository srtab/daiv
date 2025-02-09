from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import RunnableConfig  # noqa: TC002
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.base import ModelProvider
from automation.agents.image_url_extractor.agent import ImageURLExtractorAgent
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.agents.schemas import AssesmentClassification
from automation.conf import settings
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig

from .prompts import issue_addressor_human, issue_assessment_human, issue_assessment_system
from .state import OverallState

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres.base import BasePostgresSaver

logger = logging.getLogger("daiv.agents")


class IssueAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address issues created by the reporter.
    """

    def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(OverallState)

        workflow.add_node("assessment", self.assessment)
        workflow.add_node("prepare_data", self.prepare_data)
        workflow.add_node("plan_and_execute", self.plan_and_execute_subgraph(self.checkpointer, self.store))

        workflow.add_edge(START, "assessment")
        workflow.add_edge("plan_and_execute", END)

        return workflow.compile(checkpointer=self.checkpointer, store=self.store)

    def assessment(self, state: OverallState) -> Command[Literal["prepare_data", "__end__"]]:
        """
        Assess the issue created by the reporter.

        This node will help distinguish if the issue is a request to change something on the codebase or not.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Command[Literal["prepare_data", "__end__"]]: The next step in the workflow.
        """
        prompt = ChatPromptTemplate.from_messages([issue_assessment_system, issue_assessment_human])

        evaluator = prompt | self.get_model(model=settings.GENERIC_COST_EFFICIENT_MODEL_NAME).with_structured_output(
            AssesmentClassification
        ).with_fallbacks([
            self.get_model(model=settings.CODING_COST_EFFICIENT_MODEL_NAME).with_structured_output(
                AssesmentClassification
            )
        ])

        response = cast(
            "AssesmentClassification",
            evaluator.invoke({"issue_title": state["issue_title"], "issue_description": state["issue_description"]}),
        )

        if response.request_for_changes:
            return Command(goto="prepare_data")
        # TODO: ask for clarification if the issue is not a request for changes
        return Command(goto=END, update={"request_for_changes": False})

    def prepare_data(self, state: OverallState, config: RunnableConfig) -> Command[Literal["plan_and_execute"]]:
        """
        Prepare the data for the plan and execute node.
        This node will extract the images from the issue description.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Command[Literal["plan_and_execute"]]: The next step in the workflow.
        """
        codebase_index = CodebaseIndex(RepoClient.create_instance())
        repo_config = RepositoryConfig.get_config(config["configurable"]["source_repo_id"])

        extracted_images = ImageURLExtractorAgent().agent.invoke(
            {"markdown_text": state["issue_description"]},
            {
                "configurable": {
                    "repo_client_slug": config["configurable"]["repo_client"],
                    "project_id": config["configurable"]["project_id"],
                    # Anthropic models require base64 images to be sent as data URLs
                    "only_base64": IssueAddressorAgent.get_model_provider(settings.PLANING_PERFORMANT_MODEL_NAME)
                    == ModelProvider.ANTHROPIC,
                }
            },
        )

        return Command(
            goto="plan_and_execute",
            update={
                "messages": HumanMessagePromptTemplate.from_template(
                    [issue_addressor_human, *extracted_images], "jinja2"
                ).format_messages(
                    issue_title=state["issue_title"],
                    issue_description=state["issue_description"],
                    project_description=repo_config.repository_description,
                    repository_structure=codebase_index.extract_tree(
                        config["configurable"]["source_repo_id"], config["configurable"]["source_ref"]
                    ),
                )
            },
        )

    def plan_and_execute_subgraph(
        self, checkpointer: BasePostgresSaver | None, store: BaseStore | None
    ) -> CompiledStateGraph:
        """
        Compile the subgraph for the plan and execute node that will be used to address the issue.

        Args:
            store (BaseStore): The store to persist file changes.

        Returns:
            CompiledStateGraph: The compiled subgraph.
        """
        return PlanAndExecuteAgent(checkpointer=checkpointer, store=store).agent
