from __future__ import annotations

import logging
from typing import Literal, cast

from django.utils import timezone

from langchain_core.output_parsers.openai_tools import PydanticToolsParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder, SystemMessagePromptTemplate
from langchain_core.runnables import RunnableConfig, RunnablePassthrough
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore  # noqa: TC002
from langgraph.types import Command

from automation.agents import BaseAgent
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.tools.toolkits import ReadRepositoryToolkit, SandboxToolkit, WebSearchToolkit
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig

from .conf import settings
from .prompts import (
    respond_reviewer_system,
    review_assessment_human,
    review_assessment_system,
    review_plan_human,
    review_plan_system_template,
)
from .schemas import ReviewAssessment
from .state import OverallState, ReplyAgentState
from .tools import reply_reviewer_tool

logger = logging.getLogger("daiv.agents")


class ReviewAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.codebase_index = CodebaseIndex(RepoClient.create_instance())

    def compile(self) -> CompiledStateGraph:
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

        return workflow.compile(checkpointer=self.checkpointer, store=self.store)

    def assessment(self, state: OverallState) -> Command[Literal["plan_and_execute", "reply_reviewer"]]:
        """
        Assess the feedback provided by the reviewer.

        This node will help distinguish if the comments are requests to change the code or just feedback and
        define the next steps to follow.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Command[Literal["plan_and_execute", "reply_reviewer"]]: The next step in the workflow.
        """
        evaluator = (
            RunnablePassthrough.assign(
                messages=lambda inputs: inputs["messages"][:-1], comment=lambda inputs: inputs["messages"][-1].content
            )
            | ChatPromptTemplate.from_messages([
                review_assessment_system,
                MessagesPlaceholder("messages"),
                review_assessment_human,
            ])
            # We could use `with_structured_output` but it define tool_choice as "any", forcing the llm to respond with
            # a tool call without reasoning, which is crucial here to make the right decision.
            # Defining tool_choice as "auto" would let the llm to reason before calling the tool.
            | self.get_model(model=settings.ASSESSMENT_MODEL_NAME)
            .bind_tools([ReviewAssessment], tool_choice="auto")
            .with_fallbacks([
                self.get_model(model=settings.FALLBACK_ASSESSMENT_MODEL_NAME).bind_tools(
                    [ReviewAssessment], tool_choice="auto"
                )
            ])
            | PydanticToolsParser(tools=[ReviewAssessment], first_tool_only=True)
        )

        response = cast("ReviewAssessment", evaluator.invoke({"messages": state["notes"]}))

        if response.request_for_changes:
            return Command(goto="plan_and_execute", update={"requested_changes": response.requested_changes})

        return Command(goto="reply_reviewer")

    def plan_and_execute(
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

        review_plan_system = SystemMessagePromptTemplate.from_template(
            review_plan_system_template,
            "jinja2",
            partial_variables={
                "current_date_time": timezone.now().strftime("%d %B, %Y %H:%M"),
                "diff": state["diff"],
                "project_description": repo_config.repository_description,
            },
            additional_kwargs={"cache-control": {"type": "ephemeral"}},
        )

        plan_and_execute = PlanAndExecuteAgent(
            plan_system_template=review_plan_system, store=store, human_in_the_loop=False, checkpointer=False
        )

        result = plan_and_execute.agent.invoke({
            "messages": [review_plan_human.format(requested_changes=state["requested_changes"])]
        })

        if plan_questions := result.get("plan_questions"):
            return Command(goto=END, update={"reply": "\n".join(plan_questions)})
        return Command(goto=END)

    def reply_reviewer(
        self, state: OverallState, store: BaseStore, config: RunnableConfig
    ) -> Command[Literal["__end__"]]:
        """
        Compile the subgraph to reply to reviewer's comments or questions.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore | None): The store to save the state of the agent.
            config (RunnableConfig): The config for the agent.

        Returns:
            Command[Literal["__end__"]]: The next step in the workflow.
        """
        tools = (
            ReadRepositoryToolkit.create_instance().get_tools()
            + WebSearchToolkit.create_instance().get_tools()
            + SandboxToolkit.create_instance().get_tools()
        )

        react_agent = create_react_agent(
            self.get_model(model=settings.REPLY_MODEL_NAME, temperature=settings.REPLY_TEMPERATURE).with_fallbacks([
                self.get_model(model=settings.FALLBACK_REPLY_MODEL_NAME, temperature=settings.REPLY_TEMPERATURE)
            ]),
            state_schema=ReplyAgentState,
            tools=tools + [reply_reviewer_tool],
            store=store,
            checkpointer=False,
            # FIXME: Add diff hunk referenced file to the prompt to improve the agent's performance
            prompt=ChatPromptTemplate.from_messages([respond_reviewer_system, MessagesPlaceholder("messages")]),
            name="reply_reviewer_react_agent",
            version="v2",
        )

        result = react_agent.invoke({"messages": state["notes"], "diff": state["diff"]})

        # The reply is updated in the state by the reply_reviewer tool.
        # There's cases where the tool is not called, so we use the last message content as the reply.
        return Command(goto=END, update={"reply": result["messages"][-1].content})
