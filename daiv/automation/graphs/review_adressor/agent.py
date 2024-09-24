import logging
from textwrap import dedent
from typing import Literal, cast

from langchain_core.messages import SystemMessage
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from automation.graphs.agents import BaseAgent
from automation.graphs.pr_describer import PullRequestDescriberAgent, PullRequestDescriberOutput
from automation.tools import CodebaseSearchTool, ReplaceSnippetWithTool, RepositoryFileTool
from codebase.base import CodebaseChanges
from codebase.clients import AllRepoClient
from codebase.indexes import CodebaseIndex

from .prompts import (
    review_analizer_replan,
    review_analyzer_execute,
    review_analyzer_objective,
    review_analyzer_plan,
    review_analyzer_task,
)
from .schemas import Act, AskForClarification, InitialAct, Response
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

        self.codebase_changes = CodebaseChanges()
        super().__init__()

    def compile(self) -> CompiledStateGraph | Runnable:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(PlanExecute)

        workflow.add_node("plan", self.plan)
        workflow.add_node("execute_step", self.execute_step)
        workflow.add_node("replan", self.replan)
        workflow.add_node("human_feedback", self.human_feedback)
        workflow.add_node("commit_changes", self.commit_changes)

        workflow.add_edge(START, "plan")
        workflow.add_edge("execute_step", "replan")
        workflow.add_edge("human_feedback", END)
        workflow.add_edge("commit_changes", END)

        workflow.add_conditional_edges("plan", self.continue_plan_execution)
        workflow.add_conditional_edges("replan", self.continue_plan_execution)

        return workflow.compile()

    def plan(self, state: PlanExecute):
        """
        Plan the steps to follow.

        Args:
            state (PlanExecute): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        act = cast(
            InitialAct,
            self.model.with_structured_output(InitialAct).invoke(
                [
                    SystemMessage(
                        review_analyzer_plan.format(
                            diff=state["diff"], task=review_analyzer_task, objective=review_analyzer_objective
                        )
                    )
                ]
                + state["messages"]
            ),
        )

        if isinstance(act.action, Response):
            return {"response": act.action.response}
        elif isinstance(act.action, AskForClarification):
            return {"response": " ".join(act.action.questions)}
        return {"plan_steps": act.action.steps, "goal": act.action.goal, "file_changes": {}}

    def replan(self, state: PlanExecute):
        """
        Replan the steps to follow.

        Args:
            state (PlanExecute): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        act = cast(
            Act,
            self.model.with_structured_output(Act).invoke(
                [
                    review_analizer_replan.format(
                        task=review_analyzer_task,
                        objective=review_analyzer_objective,
                        plan=self.pretty_print_plan_steps(state["plan_steps"]),
                        past_steps=self.pretty_print_past_steps(state["past_steps"]),
                    )
                ]
                + state["messages"]
            ),
        )
        if isinstance(act.action, Response):
            return {"response": act.action.response, "finished": act.action.finished}
        elif isinstance(act.action, AskForClarification):
            return {"response": act.action.questions, "finished": False}
        return {"plan_steps": act.action.steps}

    def execute_step(self, state: PlanExecute):
        """
        Execute the first step of the plan.

        Args:
            state (PlanExecute): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        codebase_changes = CodebaseChanges(file_changes=state["file_changes"])

        tools = [
            CodebaseSearchTool(
                source_repo_id=self.source_repo_id, api_wrapper=CodebaseIndex(repo_client=self.repo_client)
            ),
            RepositoryFileTool(
                source_repo_id=self.source_repo_id,
                source_ref=self.source_ref,
                codebase_changes=codebase_changes,
                api_wrapper=self.repo_client,
            ),
            ReplaceSnippetWithTool(
                source_repo_id=self.source_repo_id,
                source_ref=self.source_ref,
                codebase_changes=codebase_changes,
                api_wrapper=self.repo_client,
            ),
        ]

        agent = create_react_agent(self.model, tools)
        response = agent.invoke({
            "messages": [
                SystemMessage(
                    review_analyzer_execute.format(
                        diff=state["diff"],
                        plan_steps=self.pretty_print_plan_steps(state["plan_steps"]),
                        plan_to_execute=state["plan_steps"][0],
                        goal=state["goal"],
                    )
                )
            ]
        })
        return {
            "past_steps": [(state["plan_steps"][0], response["messages"][-1].content)],
            "file_changes": codebase_changes.file_changes,
        }

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
                Estimated cost: **${total_cost:.10f}**
                """
            ).format(
                changes=changes_description.title, prompt_tokens=0, completion_tokens=0, total_tokens=0, total_cost=0
            ),
        )

    def continue_plan_execution(
        self, state: PlanExecute
    ) -> Literal["execute_step", "commit_changes", "human_feedback"]:
        """
        Determine if the agent should continue executing the plan or request feedback.

        Args:
            state (PlanExecute): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "response" in state and state["response"]:
            if state["finished"] and state["file_changes"]:
                return "commit_changes"
            return "human_feedback"
        return "execute_step"

    def pretty_print_plan_steps(self, plan_steps: list[str]) -> str:
        """
        Pretty print the plan steps.

        Args:
            plan_steps (list[str]): The plan steps.

        Returns:
            str: The pretty printed plan steps.
        """
        return "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan_steps))

    def pretty_print_past_steps(self, past_steps: list[tuple]) -> str:
        """
        Pretty print the past steps.

        Args:
            past_steps (list[tuple]): The past steps.

        Returns:
            str: The pretty printed past steps.
        """
        pprint_past_steps = "<steps>"
        for task, result in past_steps:
            pprint_past_steps += f"\n\t<step>\n\t\t<task>{task}</task>\n\t\t<result>{result}</result>\n\t</step>"
        pprint_past_steps += "</steps>"
        return pprint_past_steps
