import logging
from typing import Literal, cast

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from automation.graphs.agents import (
    CODING_PERFORMANT_MODEL_NAME,
    GENERIC_COST_EFFICIENT_MODEL_NAME,
    GENERIC_PERFORMANT_MODEL_NAME,
    BaseAgent,
)
from automation.graphs.issue_addressor.schemas import HumanFeedbackResponse
from automation.graphs.prebuilt import REACTAgent
from automation.graphs.prompts import execute_plan_human, execute_plan_system
from automation.graphs.schemas import AskForClarification, DetermineNextActionResponse, RequestAssessmentResponse
from automation.tools.toolkits import ReadRepositoryToolkit, WriteRepositoryToolkit
from codebase.base import CodebaseChanges
from codebase.clients import AllRepoClient

from .prompts import (
    human_feedback_system,
    issue_addressor_human,
    issue_addressor_system,
    issue_analyzer_assessment,
    issue_analyzer_human,
)
from .state import OverallState

logger = logging.getLogger("daiv.agents")


class IssueAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address issues created by the reporter.
    """

    def __init__(self, repo_client: AllRepoClient, *, source_repo_id: str, source_ref: str, issue_id: int, **kwargs):
        self.repo_client = repo_client
        self.source_repo_id = source_repo_id
        self.source_ref = source_ref
        self.issue_id = issue_id
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

        workflow.add_edge(START, "assessment")
        workflow.add_edge("plan", "human_feedback")
        workflow.add_edge("execute_plan", END)

        workflow.add_conditional_edges("assessment", self.continue_planning)
        workflow.add_conditional_edges("human_feedback", self.continue_executing)

        return workflow.compile(checkpointer=self.checkpointer, interrupt_before=["human_feedback"])

    def assessment(self, state: OverallState):
        """
        Assess the issue created by the reporter.

        This node will help distinguish if the issue is a request to change something on the codebase or not.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(issue_analyzer_assessment),
            HumanMessagePromptTemplate.from_template(issue_analyzer_human, "jinja2"),
        ])

        evaluator = prompt | self.model.with_structured_output(RequestAssessmentResponse, method="json_schema")

        response = cast(
            RequestAssessmentResponse,
            evaluator.invoke(
                {"issue_title": state["issue_title"], "issue_description": state["issue_description"]},
                config={"configurable": {"model": GENERIC_COST_EFFICIENT_MODEL_NAME}},
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

    def plan(self, state: OverallState):
        """
        Plan the steps to follow.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        toolkit = ReadRepositoryToolkit.create_instance(self.repo_client, self.source_repo_id, self.source_ref)

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(issue_addressor_system),
            HumanMessagePromptTemplate.from_template(issue_addressor_human, "jinja2"),
        ])
        messages = prompt.format_messages(
            issue_title=state["issue_title"], issue_description=state["issue_description"]
        )

        react_agent = REACTAgent(
            run_name="plan_react_agent",
            tools=toolkit.get_tools(),
            model_name=GENERIC_PERFORMANT_MODEL_NAME,  # PLANING_PERFORMANT_MODEL_NAME,
            with_structured_output=DetermineNextActionResponse,
        )
        result = react_agent.agent.invoke({"messages": messages})

        if isinstance(result["response"].action, AskForClarification):
            return {"response": " ".join(result["response"].action.questions)}

        return {"plan_tasks": result["response"].action.tasks, "goal": result["response"].action.goal}

    def human_feedback(self, state: OverallState):
        """
        Request human feedback.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """

        human_feedback_evaluator = self.model.with_structured_output(HumanFeedbackResponse, method="json_schema")
        result = cast(
            HumanFeedbackResponse,
            human_feedback_evaluator.invoke([SystemMessage(human_feedback_system)] + state["messages"]),
        )

        return {"response": result.feedback, "human_approved": result.is_unambiguous_approval}

    def execute_plan(self, state: OverallState):
        """
        Execute the plan by making the necessary changes to the codebase.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        codebase_changes = CodebaseChanges()
        toolkit = WriteRepositoryToolkit.create_instance(
            self.repo_client, self.source_repo_id, self.source_ref, codebase_changes
        )

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(execute_plan_system),
            HumanMessagePromptTemplate.from_template(execute_plan_human, "jinja2"),
        ])
        messages = prompt.format_messages(goal=state["goal"], plan_tasks=enumerate(state["plan_tasks"]))

        react_agent = REACTAgent(
            run_name="execute_plan_react_agent", tools=toolkit.get_tools(), model_name=CODING_PERFORMANT_MODEL_NAME
        )
        react_agent.agent.invoke({"messages": messages}, config={"configurable": {"max_tokens": 8192}})

        return {"file_changes": codebase_changes.file_changes}

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