import logging
from textwrap import dedent
from typing import Literal, cast

from langchain_core.prompts import SystemMessagePromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from automation.graphs.agents import BaseAgent
from automation.graphs.pr_describer import PullRequestDescriberAgent, PullRequestDescriberOutput
from automation.graphs.prebuilt import REACTAgent
from automation.tools.toolkits import ReadRepositoryToolkit, WriteRepositoryToolkit
from codebase.base import CodebaseChanges
from codebase.clients import AllRepoClient

from .prompts import review_analyzer_execute, review_analyzer_plan
from .schemas import ActExecuterResponse, ActPlannerResponse, AskForClarification, Response
from .state import PlanExecute

logger = logging.getLogger(__name__)


class ReviewAdressorAgent(BaseAgent):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

    model_name = "gpt-4o-2024-08-06"

    def __init__(
        self,
        repo_client: AllRepoClient,
        *,
        source_repo_id: str,
        source_ref: str,
        merge_request_id: int,
        discussion_id: str,
    ):
        self.repo_client = repo_client
        self.source_repo_id = source_repo_id
        self.source_ref = source_ref
        self.merge_request_id = merge_request_id
        self.discussion_id = discussion_id
        super().__init__()

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
        workflow = StateGraph(PlanExecute)

        workflow.add_node("plan", self.plan)
        workflow.add_node("execute_step", self.execute_step)
        workflow.add_node("human_feedback", self.human_feedback)
        workflow.add_node("commit_changes", self.commit_changes)

        workflow.add_edge(START, "plan")
        workflow.add_edge("execute_step", "commit_changes")
        workflow.add_edge("human_feedback", END)
        workflow.add_edge("commit_changes", END)

        workflow.add_conditional_edges("plan", self.continue_plan_execution)

        return workflow.compile()

    def plan(self, state: PlanExecute):
        """
        Plan the steps to follow.

        Args:
            state (PlanExecute): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        toolkit = ReadRepositoryToolkit.create_instance(self.repo_client, self.source_repo_id, self.source_ref)

        system_message_template = SystemMessagePromptTemplate.from_template(review_analyzer_plan, "jinja2")
        system_message = system_message_template.format(diff=state["diff"], messages=state["messages"])

        react_agent = REACTAgent(tools=toolkit.get_tools(), with_structured_output=ActPlannerResponse)
        response = react_agent.agent.invoke({"messages": [system_message]}, config={"callbacks": [self.usage_handler]})

        if isinstance(response["response"].action, Response):
            return {"response": response["response"].action.response}
        elif isinstance(response["response"].action, AskForClarification):
            return {"response": " ".join(response["response"].action.questions)}
        return {
            "plan_tasks": response["response"].action.tasks,
            "goal": response["response"].action.goal,
            "file_changes": {},
        }

    def execute_step(self, state: PlanExecute):
        """
        Execute the next step in the plan.

        Args:
            state (PlanExecute): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        codebase_changes = CodebaseChanges()
        toolkit = WriteRepositoryToolkit.create_instance(
            self.repo_client, self.source_repo_id, self.source_ref, codebase_changes
        )

        system_message_template = SystemMessagePromptTemplate.from_template(review_analyzer_execute, "jinja2")
        system_message = system_message_template.format(goal=state["goal"], plan_tasks=enumerate(state["plan_tasks"]))

        react_agent = REACTAgent(tools=toolkit.get_tools(), with_structured_output=ActExecuterResponse)
        response = react_agent.agent.invoke({"messages": [system_message]}, config={"callbacks": [self.usage_handler]})

        if isinstance(response["response"].action, Response):
            return {"response": response["response"].action.response, "file_changes": codebase_changes.file_changes}
        elif isinstance(response["response"].action, AskForClarification):
            return {
                "response": " ".join(response["response"].action.questions),
                "file_changes": codebase_changes.file_changes,
            }
        return {"file_changes": codebase_changes.file_changes}

    def human_feedback(self, state: PlanExecute):
        """
        Request feedback from the user by updating the merge request discussion.

        Args:
            state (PlanExecute): The state of the agent.
        """
        self.repo_client.create_merge_request_discussion_note(
            self.source_repo_id, self.merge_request_id, self.discussion_id, state["response"]
        )

    def commit_changes(self, state: PlanExecute):
        """
        Commit the changes to the codebase.

        Args:
            state (PlanExecute): The state of the agent.
        """
        self.repo_client.resolve_merge_request_discussion(
            self.source_repo_id, self.merge_request_id, self.discussion_id
        )

        pr_describer = PullRequestDescriberAgent(self.usage_handler)
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

    def continue_plan_execution(self, state: PlanExecute) -> Literal["execute_step", "human_feedback"]:
        """
        Determine if the agent should continue executing the plan or request feedback.

        Args:
            state (PlanExecute): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "response" in state and state["response"]:
            return "human_feedback"
        return "execute_step"
