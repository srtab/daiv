import logging
from textwrap import dedent
from typing import Literal, cast

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, SystemMessagePromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from automation.graphs.agents import PERFORMANT_CODING_MODEL_NAME, PERFORMANT_GENERIC_MODEL_NAME, BaseAgent
from automation.graphs.pr_describer import PullRequestDescriberAgent, PullRequestDescriberOutput
from automation.graphs.prebuilt import REACTAgent
from automation.tools.toolkits import ReadRepositoryToolkit, WriteRepositoryToolkit
from codebase.base import CodebaseChanges
from codebase.clients import AllRepoClient

from .prompts import (
    review_analyzer_assessment,
    review_analyzer_execute_human,
    review_analyzer_execute_system,
    review_analyzer_plan,
    review_analyzer_response,
)
from .schemas import AskForClarification, DetermineNextActionResponse, HumanFeedbackResponse, RequestAssessmentResponse
from .state import OverallState

logger = logging.getLogger("daiv.agents")


class ReviewAdressorAgent(BaseAgent):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

    model_name = PERFORMANT_GENERIC_MODEL_NAME

    def __init__(
        self,
        repo_client: AllRepoClient,
        *,
        source_repo_id: str,
        source_ref: str,
        merge_request_id: int,
        discussion_id: str,
        **kwargs,
    ):
        self.repo_client = repo_client
        self.source_repo_id = source_repo_id
        self.source_ref = source_ref
        self.merge_request_id = merge_request_id
        self.discussion_id = discussion_id
        super().__init__(**kwargs)

    def get_config(self) -> RunnableConfig:
        """
        Include the metadata identifying the source repository, reference, merge request, and discussion.

        Returns:
            dict: The configuration for the agent.
        """
        config = super().get_config()
        config["tags"].append(self.repo_client.client_slug)
        config["metadata"].update({
            "repo_client": self.repo_client.client_slug,
            "source_repo_id": self.source_repo_id,
            "source_ref": self.source_ref,
            "merge_request_id": self.merge_request_id,
            "discussion_id": self.discussion_id,
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
        workflow.add_edge("execute_plan", "commit_changes")
        workflow.add_edge("human_feedback", END)
        workflow.add_edge("commit_changes", END)

        workflow.add_conditional_edges("assessment", self.continue_planning)
        workflow.add_conditional_edges("plan", self.continue_executing)

        return workflow.compile()

    def assessment(self, state: OverallState):
        """
        Assess the feedback provided by the reviewer.

        This node will help distinguish if the comments are requests to change the code or just feedback and
        define the next steps to follow.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        evaluator = self.model.with_structured_output(RequestAssessmentResponse, method="json_schema")
        response = cast(
            RequestAssessmentResponse,
            evaluator.invoke([SystemMessage(review_analyzer_assessment), state["messages"][-1]]),
        )
        return {"request_for_changes": response.request_for_changes}

    def plan(self, state: OverallState):
        """
        Plan the steps to follow.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        toolkit = ReadRepositoryToolkit.create_instance(self.repo_client, self.source_repo_id, self.source_ref)

        system_message_template = SystemMessagePromptTemplate.from_template(review_analyzer_plan, "jinja2")
        system_message = system_message_template.format(diff=state["diff"])

        react_agent = REACTAgent(
            model_name=PERFORMANT_GENERIC_MODEL_NAME,
            tools=toolkit.get_tools(),
            with_structured_output=DetermineNextActionResponse,
        )
        response = react_agent.agent.invoke({"messages": [system_message] + state["messages"]})

        if isinstance(response["response"].action, AskForClarification):
            return {"response": " ".join(response["response"].action.questions)}
        return {
            "plan_tasks": response["response"].action.tasks,
            "goal": response["response"].action.goal,
            "show_diff_hunk_to_executor": response["response"].action.show_diff_hunk_to_executor,
            "file_changes": {},
        }

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
            SystemMessage(review_analyzer_execute_system),
            HumanMessagePromptTemplate.from_template(review_analyzer_execute_human, "jinja2"),
        ])
        result = prompt.invoke({
            "goal": state["goal"],
            "plan_tasks": enumerate(state["plan_tasks"]),
            "diff": state["diff"],
            "show_diff_hunk_to_executor": state["show_diff_hunk_to_executor"],
        })

        react_agent = REACTAgent(model_name=PERFORMANT_CODING_MODEL_NAME, tools=toolkit.get_tools())
        react_agent.agent.invoke({"messages": result.to_messages()})

        return {"file_changes": codebase_changes.file_changes}

    def human_feedback(self, state: OverallState):
        """
        Request human feedback to address the reviewer's comments.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        response = state.get("response")

        # this means that none of the previous steps raised a response for the reviewer
        if not response:
            toolkit = ReadRepositoryToolkit.create_instance(self.repo_client, self.source_repo_id, self.source_ref)

            system_message_template = SystemMessagePromptTemplate.from_template(review_analyzer_response, "jinja2")
            system_message = system_message_template.format(diff=state["diff"])

            react_agent = REACTAgent(tools=toolkit.get_tools(), with_structured_output=HumanFeedbackResponse)
            result = react_agent.agent.invoke({"messages": [system_message] + state["messages"]})
            response = cast(HumanFeedbackResponse, result["response"]).response

        if response:
            self.repo_client.create_merge_request_discussion_note(
                self.source_repo_id, self.merge_request_id, self.discussion_id, response
            )

        return {"response": ""}

    def commit_changes(self, state: OverallState):
        """
        Commit the changes to the codebase.

        Args:
            state (OverallState): The state of the agent.
        """
        self.repo_client.resolve_merge_request_discussion(
            self.source_repo_id, self.merge_request_id, self.discussion_id
        )

        pr_describer = PullRequestDescriberAgent()
        changes_description = cast(
            PullRequestDescriberOutput,
            pr_describer.agent.invoke([
                ". ".join(file_change.commit_messages) for file_change in state["file_changes"].values()
            ]),
        )

        self.repo_client.commit_changes(
            self.source_repo_id,
            self.source_ref,
            changes_description.commit_message,
            list(state["file_changes"].values()),
        )
        self.repo_client.comment_merge_request(
            self.source_repo_id,
            self.merge_request_id,
            dedent(
                """\
                I've made the changes: **{changes}**.

                Please review them and let me know if you need further assistance.

                ### 🤓 Stats for the nerds:
                Prompt tokens: **{prompt_tokens:,}** \\
                Completion tokens: **{completion_tokens:,}** \\
                Total tokens: **{total_tokens:,}** \\
                Successful requests: **{total_requests:,}** \\
                Total cost (USD): **${total_cost:.10f}**
                """
            ).format(
                changes=changes_description.title,
                prompt_tokens=self.usage_handler.prompt_tokens,
                completion_tokens=self.usage_handler.completion_tokens,
                total_tokens=self.usage_handler.total_tokens,
                total_requests=self.usage_handler.successful_requests,
                total_cost=self.usage_handler.total_cost,
            ),
        )

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
