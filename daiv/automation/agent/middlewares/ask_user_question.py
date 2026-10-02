from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool  # noqa: TC002
from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.prompts import ASK_USER_QUESTION_SYSTEM_PROMPT
from automation.agent.questions import (
    ASK_USER_QUESTION_TOOL_NAME,
    NOT_ALONE_ERROR,
    QUESTION_DELIVERED,
    QuestionList,
    delivered_question_call,
    render_questions,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from pydantic import ValidationError

ASK_USER_QUESTION_DESCRIPTION = (
    "Ask the user 1 to 4 questions and end your turn. Use it when the request is ambiguous in a way that changes "
    "the work, or when the decision is the user's. Offer 2 to 4 options when the choices are clear, or none for a "
    "free-text question; the user can always answer in their own words, so never add an 'Other' option. It must be "
    "the only tool call in the message. The user's answer arrives as the next user message."
)


@tool(ASK_USER_QUESTION_TOOL_NAME, description=ASK_USER_QUESTION_DESCRIPTION)
def ask_user_question(questions: QuestionList, runtime: ToolRuntime) -> ToolMessage:
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


def _format_validation_error(exc: ValidationError) -> str:
    """Turn an ``ask_user_question`` input :class:`ValidationError` into actionable retry feedback.

    Without this, a pydantic ``ValidationError`` from ``Tool._parse_input`` (e.g. a question that does not end
    with ``?``) re-raises out of ``Tool.run``/``arun`` and is reported as an unhandled error before the ToolNode
    ever gets to convert it, killing the turn's Sentry signal. Routing it through ``handle_validation_error``
    returns a ``ToolMessage`` with ``status="error"`` directly, so the agent loops back to the model with the
    validation feedback and retries with conforming input.
    """
    details = "; ".join(
        f"{'.'.join(str(loc) for loc in err['loc']) or 'input'}: {err['msg'].removeprefix('Value error, ')}"
        for err in exc.errors()
    )
    return f"Invalid {ASK_USER_QUESTION_TOOL_NAME} arguments: {details}. Fix the arguments and call the tool again."


# Recoverable input validation: a non-conforming question (e.g. not ending in '?') becomes a tool-error
# message the model retries on, instead of an unhandled ValidationError that aborts the chat turn.
ask_user_question.handle_validation_error = _format_validation_error


class AskUserQuestionMiddleware(AgentMiddleware):
    """Binds ``ask_user_question`` and, once a question is delivered, answers the next model call with its rendering.

    Not ``return_direct``: that would also end the turn on an invalid call, which must loop back to the model.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tools = [ask_user_question]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        if (call := delivered_question_call(request.messages)) is not None:
            return AIMessage(content=render_questions(call["args"]))
        section = ASK_USER_QUESTION_SYSTEM_PROMPT
        system_prompt = f"{request.system_prompt}\n\n{section}" if request.system_prompt else section
        return await handler(request.override(system_prompt=system_prompt))
