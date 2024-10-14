import logging
from typing import Literal, cast

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from automation.graphs.agents import (
    CODING_PERFORMANT_MODEL_NAME,
    GENERIC_COST_EFFICIENT_MODEL_NAME,
    PLANING_PERFORMANT_MODEL_NAME,
    BaseAgent,
)
from automation.graphs.prebuilt import REACTAgent
from automation.graphs.schemas import AskForClarification, DetermineNextActionResponse, RequestAssessmentResponse
from automation.tools.toolkits import ReadRepositoryToolkit, WriteRepositoryToolkit
from codebase.base import CodebaseChanges, Issue, IssueType
from codebase.clients import AllRepoClient
from core.constants import BOT_LABEL

from .prompts import issue_addressor_human, issue_addressor_system, issue_analyzer_assessment, issue_analyzer_human
from .state import OverallState

logger = logging.getLogger("daiv.agents")

REVIEW_PLAN_TEMPLATE = """📌 **Please take a moment to examine the plan.**

- **Modify Tasks:** You can add, delete, or adjust tasks as needed. Customized tasks will be considered when executing the plan.
- **Plan Adjustments:** If the plan doesn't meet your expectations, please refine the issue description and add more details or examples to help me understand the problem better. I will then replan the tasks and delete the existing ones.
- **Approval:** If everything looks good, please reply directly to this comment with your approval, and I'll proceed.

---

Thank you! 😊
"""  # noqa: E501


class IssueAddressorAgent(BaseAgent):
    """ """

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

    def compile(self) -> CompiledStateGraph | Runnable:
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
        workflow.add_node("commit_changes", self.commit_changes)

        workflow.add_edge(START, "assessment")
        workflow.add_edge("plan", "human_feedback")
        workflow.add_edge("execute_plan", "commit_changes")
        workflow.add_edge("human_feedback", END)
        workflow.add_edge("commit_changes", END)

        workflow.add_conditional_edges("assessment", self.continue_planning)
        workflow.add_conditional_edges("plan", self.continue_executing)

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
        # Check if the issue is already planned, if so, skip the assessment
        if state.get("plan_tasks"):
            return {"request_for_changes": False}

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(issue_analyzer_assessment),
            HumanMessagePromptTemplate.from_template(issue_analyzer_human, "jinja2"),
        ])

        evaluator = prompt | self.model.with_structured_output(RequestAssessmentResponse, method="json_schema")

        response = cast(
            RequestAssessmentResponse,
            evaluator.invoke(
                {"issue_title": state["issue"].title, "issue_description": state["issue"].description},
                config={"configurable": {"model": GENERIC_COST_EFFICIENT_MODEL_NAME}},
            ),
        )
        return {"request_for_changes": response.request_for_changes}

    def continue_planning(self, state: OverallState) -> Literal["plan", "human_feedback", "execute_plan"]:
        """
        Check if the agent should continue planning or provide/request human feedback.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "plan_tasks" in state and state["plan_tasks"]:
            return "execute_plan"
        if "request_for_changes" in state and state["request_for_changes"]:
            return "plan"
        if "human_approved" in state and state["human_approved"]:
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
            issue_title=state["issue"].title, issue_description=state["issue"].description
        )

        react_agent = REACTAgent(
            run_name="plan_react_agent",
            tools=toolkit.get_tools(),
            model_name=PLANING_PERFORMANT_MODEL_NAME,
            with_structured_output=DetermineNextActionResponse,
        )
        result = react_agent.agent.invoke({"messages": messages})

        if isinstance(result["response"].action, AskForClarification):
            return {"response": " ".join(result["response"].action.questions)}

        return {"plan_tasks": result["response"].action.tasks, "goal": result["response"].action.goal}

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
            SystemMessage("review_analyzer_execute_system"),
            HumanMessagePromptTemplate.from_template("review_analyzer_execute_human", "jinja2"),
        ])
        messages = prompt.format_messages({"goal": state["goal"], "plan_tasks": enumerate(state["plan_tasks"])})

        react_agent = REACTAgent(
            run_name="execute_plan_react_agent", tools=toolkit.get_tools(), model_name=CODING_PERFORMANT_MODEL_NAME
        )
        react_agent.agent.invoke({"messages": messages})

        return {"file_changes": codebase_changes.file_changes}

    def human_feedback(self, state: OverallState):
        """
        Request human feedback.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        issue_iid = cast(int, state["issue"].iid)

        if response := state.get("response"):
            self.repo_client.comment_issue(self.source_repo_id, issue_iid, response)
        elif plan_tasks := state.get("plan_tasks"):
            issue_id = cast(int, state["issue"].id)
            issue_tasks = [
                Issue(
                    title=plan_task,
                    assignee=self.repo_client.current_user,
                    issue_type=IssueType.TASK,
                    labels=[BOT_LABEL],
                )
                for plan_task in plan_tasks
            ]

            self.repo_client.create_issue_tasks(self.source_repo_id, issue_id, issue_tasks)
            self.repo_client.comment_issue(self.source_repo_id, issue_iid, REVIEW_PLAN_TEMPLATE)

        return {"response": ""}

    def commit_changes(self, state: OverallState):
        """
        Commit the changes to the codebase.

        Args:
            state (OverallState): The state of the agent.
        """
        # TODO: Implement the commit changes logic

    def continue_executing(self, state: OverallState) -> Literal["execute_plan", "human_feedback"]:
        """
        Check if the agent should continue executing the plan or request human feedback

        Args:
            state (OverallState): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "response" in state and state["response"]:
            return "human_feedback"
        return "execute_plan"
