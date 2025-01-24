from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.store.memory import InMemoryStore

from automation.agents import BaseAgent
from automation.agents.base import ModelProvider
from automation.agents.image_url_extractor.agent import ImageURLExtractorAgent
from automation.agents.issue_addressor.schemas import HumanFeedbackResponse
from automation.agents.prebuilt import REACTAgent, prepare_repository_files_as_messages
from automation.agents.prompts import execute_plan_human, execute_plan_system
from automation.agents.schemas import AskForClarification, AssesmentClassificationResponse, DetermineNextActionResponse
from automation.conf import settings
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import ReadRepositoryToolkit, SandboxToolkit, WebSearchToolkit, WriteRepositoryToolkit
from automation.utils import file_changes_namespace
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig

from .prompts import (
    human_feedback_system,
    issue_addressor_human,
    issue_addressor_system,
    issue_assessment_human,
    issue_assessment_system,
)
from .state import OverallState

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from codebase.base import FileChange
    from codebase.clients import AllRepoClient

logger = logging.getLogger("daiv.agents")


class IssueAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address issues created by the reporter.
    """

    def __init__(
        self,
        repo_client: AllRepoClient,
        *,
        project_id: int,
        source_repo_id: str,
        source_ref: str,
        issue_id: int,
        **kwargs,
    ):
        self.repo_client = repo_client
        # TODO: pass this parameters as part of the config instead of the constructor
        self.project_id = project_id
        self.source_repo_id = source_repo_id
        self.source_ref = source_ref
        self.issue_id = issue_id
        self.repo_config = RepositoryConfig.get_config(self.source_repo_id)
        self.codebase_index = CodebaseIndex(self.repo_client)
        super().__init__(**kwargs)

    def get_config(self) -> RunnableConfig:
        """
        Include the metadata identifying the source repository and issue.

        Returns:
            dict: The configuration for the agent.
        """
        config = super().get_config()
        config["tags"].append(self.repo_client.client_slug)
        config["metadata"].update({
            "repo_client": self.repo_client.client_slug,
            "source_repo_id": self.source_repo_id,
            "source_ref": self.source_ref,
            "issue_id": self.issue_id,
        })
        return config

    def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(OverallState)

        workflow.add_node("assessment", self.assessment)
        workflow.add_node("plan", self.plan)
        workflow.add_node("execute_plan", self.execute_plan)
        workflow.add_node("human_feedback", self.human_feedback)
        workflow.add_node("apply_lint_fix", self.apply_lint_fix)

        workflow.add_edge(START, "assessment")
        workflow.add_edge("plan", "human_feedback")
        workflow.add_conditional_edges(
            "execute_plan",
            self.determine_if_lint_fix_should_be_applied,
            {"apply_lint_fix": "apply_lint_fix", "end": END},
        )
        workflow.add_edge("apply_lint_fix", END)

        workflow.add_conditional_edges("assessment", self.continue_planning)
        workflow.add_conditional_edges("human_feedback", self.continue_executing)

        in_memory_store = InMemoryStore()

        return workflow.compile(
            checkpointer=self.checkpointer, interrupt_before=["human_feedback"], store=in_memory_store
        )

    def assessment(self, state: OverallState):
        """
        Assess the issue created by the reporter.

        This node will help distinguish if the issue is a request to change something on the codebase or not.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        prompt = ChatPromptTemplate.from_messages([issue_assessment_system, issue_assessment_human])

        evaluator = prompt | self.model.with_structured_output(AssesmentClassificationResponse)

        response = cast(
            "AssesmentClassificationResponse",
            evaluator.invoke(
                {"issue_title": state["issue_title"], "issue_description": state["issue_description"]},
                config={"configurable": {"model": settings.GENERIC_COST_EFFICIENT_MODEL_NAME}},
            ),
        )
        return {"request_for_changes": response.request_for_changes}

    def continue_planning(self, state: OverallState) -> Literal["plan", "human_feedback"]:
        """
        Check if the agent should continue planning or provide/request human feedback.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "request_for_changes" in state and state["request_for_changes"]:
            return "plan"
        return "human_feedback"

    def plan(self, state: OverallState, store: BaseStore):
        """
        Plan the steps to follow.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        tools = (
            ReadRepositoryToolkit.create_instance(self.repo_client, self.source_repo_id, self.source_ref).get_tools()
            + WebSearchToolkit.create_instance().get_tools()
            + SandboxToolkit.create_instance().get_tools()
        )

        extracted_images = ImageURLExtractorAgent().agent.invoke(
            {"markdown_text": state["issue_description"]},
            {
                "configurable": {
                    "repo_client_slug": self.repo_client.client_slug,
                    "project_id": self.project_id,
                    # Anthropic models require base64 images to be sent as data URLs
                    "only_base64": IssueAddressorAgent.get_model_provider(settings.PLANING_PERFORMANT_MODEL_NAME)
                    == ModelProvider.ANTHROPIC,
                }
            },
        )

        prompt = ChatPromptTemplate.from_messages([
            issue_addressor_system,
            HumanMessagePromptTemplate.from_template([issue_addressor_human, *extracted_images], "jinja2"),
        ])

        messages = prompt.format_messages(
            issue_title=state["issue_title"],
            issue_description=state["issue_description"],
            project_description=self.repo_config.repository_description,
            repository_structure=self.codebase_index.extract_tree(self.source_repo_id, self.source_ref),
            tools=[tool.name for tool in tools],
            recursion_limit=settings.RECURSION_LIMIT,
        )

        react_agent = REACTAgent(
            run_name="plan_react_agent",
            tools=tools,
            model_name=settings.PLANING_PERFORMANT_MODEL_NAME,
            fallback_model_name=settings.GENERIC_PERFORMANT_MODEL_NAME,
            with_structured_output=DetermineNextActionResponse,
            store=store,
        )
        result = react_agent.agent.invoke({"messages": messages}, config={"recursion_limit": settings.RECURSION_LIMIT})

        if "response" not in result:
            return {"response": ""}

        if isinstance(result["response"].action, AskForClarification):
            return {"questions": result["response"].action.questions}

        return {"plan_tasks": result["response"].action.tasks, "goal": result["response"].action.goal}

    def human_feedback(self, state: OverallState):
        """
        Request human feedback.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """

        human_feedback_evaluator = self.model.with_structured_output(HumanFeedbackResponse)
        result = cast(
            "HumanFeedbackResponse",
            human_feedback_evaluator.invoke(
                [human_feedback_system] + state["messages"],
                config={"configurable": {"model": settings.GENERIC_COST_EFFICIENT_MODEL_NAME}},
            ),
        )

        return {"response": result.feedback, "human_approved": result.is_unambiguous_approval}

    def execute_plan(self, state: OverallState, store: BaseStore):
        """
        Execute the plan by making the necessary changes to the codebase.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        tools = (
            WriteRepositoryToolkit.create_instance(self.repo_client, self.source_repo_id, self.source_ref).get_tools()
            + SandboxToolkit.create_instance().get_tools()
        )

        prompt = ChatPromptTemplate.from_messages(
            [execute_plan_system, execute_plan_human]
            + prepare_repository_files_as_messages(
                self.repo_client,
                self.source_repo_id,
                self.source_ref,
                [task.path for task in state["plan_tasks"]],
                store,
            )
        )

        messages = prompt.format_messages(
            goal=state["goal"],
            plan_tasks=enumerate(state["plan_tasks"]),
            project_description=self.repo_config.repository_description,
            repository_structure=self.codebase_index.extract_tree(self.source_repo_id, self.source_ref),
        )

        react_agent = REACTAgent(
            run_name="execute_plan_react_agent",
            tools=tools,
            model_name=settings.CODING_PERFORMANT_MODEL_NAME,
            fallback_model_name=settings.GENERIC_PERFORMANT_MODEL_NAME,
            store=store,
        )
        react_agent.agent.invoke({"messages": messages}, config={"recursion_limit": settings.RECURSION_LIMIT})

    def determine_if_lint_fix_should_be_applied(
        self, state: OverallState, store: BaseStore
    ) -> Literal["apply_lint_fix", "end"]:
        """
        Determine whether the lint fix should be applied after the plan has been executed.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Literal["apply_lint_fix", "end"]: The next step in the workflow.
        """
        return (
            "apply_lint_fix"
            if self.repo_config.commands.enabled()
            and store.search(file_changes_namespace(self.source_repo_id, self.source_ref), limit=1)
            else "end"
        )

    def apply_lint_fix(self, state: OverallState, store: BaseStore):
        """
        Apply lint fix to the file changes made by the agent.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
        """
        run_command_tool = RunSandboxCommandsTool(
            source_repo_id=self.source_repo_id, source_ref=self.source_ref, api_wrapper=self.repo_client
        )
        run_command_tool.invoke({
            "commands": [self.repo_config.commands.install_dependencies, self.repo_config.commands.format_code],
            "intent": "Fix linting issues",
            "store": store,
        })

    def continue_executing(self, state: OverallState) -> Literal["execute_plan", "human_feedback"]:
        """
        Check if the agent should continue executing the plan or request human feedback

        Args:
            state (OverallState): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "human_approved" in state and state["human_approved"]:
            return "execute_plan"
        return "human_feedback"

    def get_files_to_commit(self) -> list[FileChange]:
        if self.agent.store is None:
            return []
        return [
            cast("FileChange", item.value["data"])
            for item in self.agent.store.search(file_changes_namespace(self.source_repo_id, self.source_ref))
        ]
