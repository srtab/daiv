from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool  # noqa: TC002
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field

from automation.agent.prompts import ASK_USER_DISABLED_SYSTEM_PROMPT, ASK_USER_QUESTION_SYSTEM_PROMPT
from automation.agent.questions import (
    ASK_USER_QUESTION_TOOL_NAME,
    NOT_ALONE_ERROR,
    QUESTION_DELIVERED,
    AskUserQuestionInput,
    Question,
    delivered_question_call,
    render_questions,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult

ASK_USER_QUESTION_DESCRIPTION = (
    "Ask the user 1 to 4 questions and end your turn. Use it when the request is ambiguous in a way that changes "
    "the work, or when the decision is the user's. Offer 2 to 4 options when the choices are clear, or none for a "
    "free-text question; the user can always answer in their own words, so never add an 'Other' option. It must be "
    "the only tool call in the message. The user's answer arrives as the next user message."
)


@tool(ASK_USER_QUESTION_TOOL_NAME, description=ASK_USER_QUESTION_DESCRIPTION)
def ask_user_question(
    questions: Annotated[list[Question], Field(min_length=1, max_length=4)], runtime: ToolRuntime
) -> ToolMessage:
    caller = next(
        (
            message
            for message in reversed(runtime.state["messages"])
            if isinstance(message, AIMessage) and any(call["id"] == runtime.tool_call_id for call in message.tool_calls)
        ),
        None,
    )
    if caller is None or len(caller.tool_calls) != 1:
        return ToolMessage(
            content=NOT_ALONE_ERROR, tool_call_id=runtime.tool_call_id, name=ASK_USER_QUESTION_TOOL_NAME, status="error"
        )
    return ToolMessage(content=QUESTION_DELIVERED, tool_call_id=runtime.tool_call_id, name=ASK_USER_QUESTION_TOOL_NAME)


class AskUserQuestionMiddleware(AgentMiddleware):
    """Binds ``ask_user_question`` and ends the turn once a question is delivered.

    The turn ends by answering the model call after a delivery with a tool-call-free ``AIMessage`` carrying the
    rendered question, so the graph routes to the turn end and every ``after_agent`` hook still runs. A tool with
    ``return_direct=True`` would also end the turn on an invalid call, which must instead loop back to the model.
    The message is deliberately not streamed: the chat renders the question from the tool call's arguments.

    Disabled, it binds nothing and tells the model nobody can answer during the run.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__()
        self.enabled = enabled
        self.tools = [ask_user_question] if enabled else []

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        if self.enabled and (call := delivered_question_call(request.messages)) is not None:
            payload = AskUserQuestionInput.model_validate(call["args"]).model_dump()
            return AIMessage(content=render_questions(payload))
        section = ASK_USER_QUESTION_SYSTEM_PROMPT if self.enabled else ASK_USER_DISABLED_SYSTEM_PROMPT
        system_prompt = f"{request.system_prompt}\n\n{section}" if request.system_prompt else section
        return await handler(request.override(system_prompt=system_prompt))
