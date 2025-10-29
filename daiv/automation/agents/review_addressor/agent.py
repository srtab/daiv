from __future__ import annotations

import logging
import uuid
from textwrap import dedent
from typing import TYPE_CHECKING

from django.conf import settings as django_settings
from django.utils import timezone

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain.tools import ToolRuntime
from langchain_anthropic.middleware.prompt_caching import AnthropicPromptCachingMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.config import get_stream_writer
from langgraph.constants import START
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from automation.agents import BaseAgent
from automation.agents.middleware import InjectImagesMiddleware
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from automation.agents.tools.sandbox import bash_tool
from automation.agents.tools.toolkits import FileNavigationToolkit, MergeRequestToolkit, WebSearchToolkit
from automation.utils import has_file_changes
from codebase.context import RuntimeCtx
from core.constants import BOT_NAME

from .conf import settings
from .prompts import respond_reviewer_system, review_comment_system, review_human
from .schemas import ReviewCommentEvaluation, ReviewCommentInput
from .state import OverallState, ReplyAgentState, ReplyReviewerState, ReviewInState, ReviewOutState

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

    from codebase.managers.review_addressor import ReviewContext

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
        ).with_config({"run_name": "review_comment_evaluator"})


@dynamic_prompt
def respond_reviewer_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic prompt for the respond reviewer system.

    Args:
        request (ModelRequest): The request to the model.

    Returns:
        str: The dynamic prompt for the plan system.
    """
    return respond_reviewer_system.format(
        current_date_time=timezone.now().strftime("%d %B, %Y"),
        bot_name=BOT_NAME,
        bot_username=request.runtime.context.bot_username,
        tools_names=[tool.name for tool in request.tools],
        repository=request.runtime.context.repo_id,
        diff=request.state["diff"],
    ).content


class ReplyReviewerAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to reply to reviewer's comments or questions.
    """

    async def compile(self) -> CompiledStateGraph:
        tools = FileNavigationToolkit.get_tools() + WebSearchToolkit.get_tools() + MergeRequestToolkit.get_tools()
        return create_agent(
            BaseAgent.get_model(model=settings.REPLY_MODEL_NAME, temperature=settings.REPLY_TEMPERATURE),
            state_schema=ReplyAgentState,
            context_schema=RuntimeCtx,
            tools=tools,
            store=self.store,
            checkpointer=self.checkpointer,
            middleware=[respond_reviewer_system_prompt, InjectImagesMiddleware(), AnthropicPromptCachingMiddleware()],
            name="reply_reviewer_agent",
        )


class ReviewAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

    def __init__(self, *, skip_format_code: bool = False, **kwargs):
        self.skip_format_code = skip_format_code
        super().__init__(**kwargs)

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(
            OverallState, input_schema=ReviewInState, output_schema=ReviewOutState, context_schema=RuntimeCtx
        )

        workflow.add_node("evaluate_review_comments", self.evaluate_review_comments)
        workflow.add_node("collect_evaluations", self.collect_evaluations, defer=True)
        workflow.add_node("plan_and_execute_processor", self.plan_and_execute_processor)
        workflow.add_node("reply_reviewer", self.reply_reviewer)
        workflow.add_node("final_aggregate", self.final_aggregate, defer=True)

        if not self.skip_format_code:
            workflow.add_node("apply_format_code", self.apply_format_code)

        workflow.add_conditional_edges(START, self.route_to_evaluate_review_comments, ["evaluate_review_comments"])
        workflow.add_edge("evaluate_review_comments", "collect_evaluations")
        workflow.add_conditional_edges(
            "collect_evaluations", self.route_to_processors, ["plan_and_execute_processor", "reply_reviewer"]
        )
        workflow.add_edge("plan_and_execute_processor", "final_aggregate")
        workflow.add_edge("reply_reviewer", "final_aggregate")

        if not self.skip_format_code:
            workflow.add_edge("final_aggregate", "apply_format_code")
            workflow.add_edge("apply_format_code", END)
        else:
            workflow.add_edge("final_aggregate", END)

        return workflow.compile(checkpointer=self.checkpointer, store=self.store, name=settings.NAME)

    async def route_to_evaluate_review_comments(self, state: ReviewInState) -> list[Send]:
        """
        Route to the evaluate review comments node for parallel processing.

        Args:
            state (ReviewInState): The state of the agent containing the reviews to evaluate.

        Returns:
            list[Send]: The sends to the evaluate review comments node for parallel processing.
        """
        return [
            Send("evaluate_review_comments", {"review_context": review_context})
            for review_context in state["to_review"]
        ]

    async def evaluate_review_comments(self, state: ReplyReviewerState) -> dict:
        """
        Assess the feedback provided by the reviewer.

        This node will help distinguish if the comments are requests to change the code or just feedback and
        define the next steps to follow.

        Args:
            state (ReviewState): The state of the agent.

        Returns:
            dict: The next step in the workflow.
        """
        review_comment_evaluator = await ReviewCommentEvaluator.get_runnable()
        response = await review_comment_evaluator.ainvoke({"messages": state["review_context"].notes})

        if response.request_for_changes:
            return {"to_plan_and_execute": [state["review_context"]]}

        return {"to_reply": [state["review_context"]]}

    async def collect_evaluations(self, state: OverallState) -> dict:
        """
        Collect the results of all assessment nodes.

        This deferred node waits for all parallel assessment tasks to complete
        and collects the classified reviews into to_reply and to_plan_and_execute lists.
        """
        # Just pass through - the state reducers will have collected everything
        return {}

    async def route_to_processors(self, state: OverallState) -> list[Send]:
        """
        Route classified reviews to appropriate processors.

        - to_reply reviews are sent to reply_reviewer in parallel (one Send per review)
        - to_plan_and_execute reviews are sent as a batch to sequential_processor
        """
        sends = []

        # Fan out to_reply reviews in parallel
        for review_context in state.get("to_reply", []):
            sends.append(Send("reply_reviewer", {"review_context": review_context}))

        # Send to_plan_and_execute as a batch for sequential processing
        if state.get("to_plan_and_execute"):
            sends.append(Send("plan_and_execute_processor", state))

        return sends

    async def plan_and_execute_processor(self, state: OverallState, runtime: Runtime[RuntimeCtx]) -> dict:
        """
        Process plan_and_execute reviews sequentially to avoid conflicts.

        Each review that requires code changes is processed one at a time,
        ensuring that file modifications don't conflict with each other.
        """
        stream_writer = get_stream_writer()

        for review_context in state.get("to_plan_and_execute", []):
            stream_writer({"plan_and_execute": "starting", "review_context": review_context})

            result = await self._plan_and_execute(review_context, runtime)

            completed_data = {"review_context": review_context, "plan_and_execute": "completed"}

            if plan_questions := result.get("plan_questions"):
                completed_data["reply"] = plan_questions
            elif no_changes_needed := result.get("no_changes_needed"):
                completed_data["reply"] = no_changes_needed
            elif "plan_tasks" in result and result["messages"][-1].type == "ai":
                # if a plan has been generated, means that the changes have been applied
                completed_data["reply"] = result["messages"][-1].content

            stream_writer(completed_data)
        return {}

    async def final_aggregate(self, state: OverallState) -> dict:
        """
        Final aggregation of all results from parallel and sequential processing.

        This deferred node waits for both reply_reviewer and sequential_processor
        to complete and collects all replies.
        """
        # All replies have been collected via the state reducers
        return {}

    async def _plan_and_execute(self, review_context: ReviewContext, runtime: Runtime[RuntimeCtx]) -> dict:
        """
        Node to plan and execute the changes requested by the reviewer.

        Args:
            review_context (ReviewContext): The review context.
            store (BaseStore): The store to persist file changes.

        Returns:
            dict: Result containing the plan questions.
        """
        plan_and_execute_agent = await PlanAndExecuteAgent.get_runnable(
            store=runtime.store,
            skip_approval=True,
            skip_format_code=True,  # we will apply format code after all reviews are addressed
            checkpointer=False,
        )

        review_human_messages = await review_human.aformat_messages(
            diff=review_context.diff, reviewer_comment=review_context.notes[0].content
        )
        return await plan_and_execute_agent.ainvoke(
            {"messages": review_human_messages + review_context.notes[1:]}, context=runtime.context
        )

    async def reply_reviewer(self, state: ReplyReviewerState, runtime: Runtime[RuntimeCtx]) -> dict:
        """
        Reply to reviewer's comments or questions.

        Args:
            state (ReplyReviewerState): The state of the agent containing the review context to reply to.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict: Result containing the reply generated during the workflow.
        """
        async with AsyncPostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            reply_reviewer_agent = await ReplyReviewerAgent.get_runnable(store=runtime.store, checkpointer=checkpointer)
            result = await reply_reviewer_agent.ainvoke(
                {"messages": state["review_context"].notes, "diff": state["review_context"].diff},
                context=runtime.context,
                config={"configurable": {"thread_id": state["review_context"].discussion.id}},
            )

        stream_writer = get_stream_writer()
        stream_writer({"reply": result["messages"][-1].content, "review_context": state["review_context"]})

        return {}

    async def apply_format_code(
        self, state: OverallState, config: RunnableConfig, runtime: Runtime[RuntimeCtx]
    ) -> dict:
        """
        Apply format code to the file changes.
        """
        if not await has_file_changes(runtime.store):
            return {}

        tool_call_id = uuid.uuid4()
        result = await bash_tool.ainvoke({
            "type": "tool_call",
            "name": bash_tool.name,
            "id": tool_call_id,
            "args": {
                "commands": runtime.context.config.sandbox.format_code,
                "runtime": ToolRuntime[RuntimeCtx](
                    state=state,
                    tool_call_id=tool_call_id,
                    config=config,
                    context=runtime.context,
                    store=runtime.store,
                    stream_writer=runtime.stream_writer,
                ),
            },
        })

        # If the command failed to format the code, we need to ask the user to agent to try to fix the formatting errors
        if result.artifact is not None and result.artifact.exit_code != 0:
            plan_and_execute_agent = await PlanAndExecuteAgent.get_runnable(
                store=runtime.store, checkpointer=self.checkpointer
            )
            prompt = dedent("""\
                The following command failed to format the code. Analyze the output and fix the formatting errors:

                <command>{result.artifact.command}</command>
                <exit_code>{result.artifact.exit_code}</exit_code>
                <output>
                    {result.artifact.output}
                </output>""")
            await plan_and_execute_agent.ainvoke({"messages": [HumanMessage(content=prompt)]}, context=runtime.context)
        return {}
