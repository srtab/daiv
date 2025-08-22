from __future__ import annotations

import logging
from typing import Literal

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder, jinja2_formatter
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.agents.plan_and_execute.prompts import plan_system
from automation.tools import think
from automation.tools.toolkits import FileNavigationToolkit, WebSearchToolkit
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig
from core.constants import BOT_NAME

from .conf import settings
from .prompts import (
    respond_reviewer_system,
    review_comment_system,
    review_plan_system_after_rules,
    review_plan_system_before_workflow,
    review_plan_system_role,
)
from .schemas import ReviewCommentEvaluation, ReviewCommentInput
from .state import OverallState, ReplyAgentState

logger = logging.getLogger("daiv.agents")


class ReviewCommentEvaluator(BaseAgent[Runnable[ReviewCommentInput, ReviewCommentEvaluation]]):
    """
    Agent to evaluate if a review comment is a request for changes to the codebase.
    """

    async def compile(self) -> Runnable:
        return (
            ChatPromptTemplate.from_messages([review_comment_system, MessagesPlaceholder("messages")])
            | BaseAgent.get_model(model=settings.REVIEW_COMMENT_MODEL_NAME).with_structured_output(
                ReviewCommentEvaluation
            )
        ).with_config({"run_name": "ReviewCommentEvaluator"})


class ReplyReviewerAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to reply to reviewer's comments or questions.
    """

    async def compile(self) -> CompiledStateGraph:
        tools = FileNavigationToolkit.get_tools() + WebSearchToolkit.get_tools()
        repo_client = RepoClient.create_instance()

        return create_react_agent(
            BaseAgent.get_model(model=settings.REPLY_MODEL_NAME, temperature=settings.REPLY_TEMPERATURE),
            state_schema=ReplyAgentState,
            tools=tools + [think],
            store=self.store,
            checkpointer=False,
            prompt=ChatPromptTemplate.from_messages([respond_reviewer_system, MessagesPlaceholder("messages")]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"),
                bot_name=BOT_NAME,
                bot_username=repo_client.current_user.username,
            ),
            name=settings.REPLY_NAME,
            version="v2",
        )


class ReviewAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.codebase_index = CodebaseIndex(RepoClient.create_instance())

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(OverallState)

        workflow.add_node("assessment", self.assessment)
        workflow.add_node("plan_and_execute", self.plan_and_execute)
        workflow.add_node("reply_reviewer", self.reply_reviewer)

        workflow.set_entry_point("assessment")

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def assessment(self, state: OverallState) -> Command[Literal["plan_and_execute", "reply_reviewer"]]:
        """
        Assess the feedback provided by the reviewer.

        This node will help distinguish if the comments are requests to change the code or just feedback and
        define the next steps to follow.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Command[Literal["plan_and_execute", "reply_reviewer"]]: The next step in the workflow.
        """
        review_comment_evaluator = await ReviewCommentEvaluator.get_runnable()
        response = await review_comment_evaluator.ainvoke({"messages": state["notes"]})

        if response.request_for_changes:
            return Command(goto="plan_and_execute")

        return Command(goto="reply_reviewer")

    async def plan_and_execute(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Node to plan and execute the changes requested by the reviewer.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to persist file changes.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        repo_config = RepositoryConfig.get_config(config["configurable"]["source_repo_id"])

        plan_system.prompt = plan_system.prompt.partial(
            role=review_plan_system_role,
            before_workflow=review_plan_system_before_workflow,
            after_rules=jinja2_formatter(
                review_plan_system_after_rules,
                project_description=repo_config.repository_description,
                diff=state["diff"],
            ),
        )

        plan_and_execute = await PlanAndExecuteAgent(
            plan_system_template=plan_system,
            store=store,
            skip_approval=True,
            skip_format_code=True,  # we will apply format code after all reviews are addressed
            checkpointer=False,
        )._runnable

        result = await plan_and_execute.ainvoke({"messages": state["notes"]})

        if plan_questions := result.get("plan_questions"):
            return Command(goto=END, update={"reply": plan_questions})
        return Command(goto=END)

    async def reply_reviewer(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Reply to reviewer's comments or questions.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore | None): The store to save the state of the agent.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        reply_reviewer_agent = await ReplyReviewerAgent(store=store)._runnable

        result = await reply_reviewer_agent.ainvoke({"messages": state["notes"], "diff": state["diff"]})

        return Command(goto=END, update={"reply": result["messages"][-1].content})
