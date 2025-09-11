from __future__ import annotations

import logging
from typing import Literal

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.agents.tools import think_tool
from automation.agents.tools.toolkits import FileNavigationToolkit, WebSearchToolkit
from codebase.clients import RepoClient
from core.constants import BOT_NAME

from .conf import settings
from .prompts import respond_reviewer_system, review_comment_system, review_human
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
            tools=tools + [think_tool],
            store=self.store,
            checkpointer=False,
            prompt=ChatPromptTemplate.from_messages([respond_reviewer_system, MessagesPlaceholder("messages")]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"),
                bot_name=BOT_NAME,
                bot_username=repo_client.current_user.username,
            ),
            name="reply_reviewer_react_agent",
        )


class ReviewAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

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

        plan_and_execute = await PlanAndExecuteAgent.get_runnable(
            store=store,
            skip_approval=True,
            skip_format_code=True,  # we will apply format code after all reviews are addressed
            checkpointer=False,
        )

        review_human_messages = review_human.aformat_messages(
            diff=state["diff"], reviewer_comment=state["notes"][0].content
        )
        result = await plan_and_execute.ainvoke({"messages": review_human_messages + state["notes"][1:]})

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
        reply_reviewer_agent = await ReplyReviewerAgent.get_runnable(store=store)

        result = await reply_reviewer_agent.ainvoke({"messages": state["notes"], "diff": state["diff"]})

        return Command(goto=END, update={"reply": result["messages"][-1].content})
