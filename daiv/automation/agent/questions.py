"""The ask-the-user question: its schema, its rendering, and how a thread shows one is pending.

A turn that asks leaves three messages at the end of the checkpoint: the ``AIMessage`` whose only tool call is
``ask_user_question``, the tool's successful ``ToolMessage`` (content exactly ``QUESTION_DELIVERED``), and the
tool-call-free message ``AskUserQuestionMiddleware`` answers the next model call with. That tail is the whole
record; nothing is stored in agent state, and the user's next message clears it by no longer being last.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import AnyMessage, ToolCall

ASK_USER_QUESTION_TOOL_NAME = "ask_user_question"

QUESTION_DELIVERED = "Question delivered to the user. This turn is over; their answer arrives as the next user message."
NOT_ALONE_ERROR = f"`{ASK_USER_QUESTION_TOOL_NAME}` must be the only tool call in the message; nothing was asked."

_FREE_TEXT_HINT = "_If none of the options fit, reply in your own words._"
_MULTI_SELECT_HINT = "_Pick any that apply._"


class Option(BaseModel):
    label: str = Field(min_length=1, description="The choice as the user sees it: 1 to 5 words.")
    description: str = Field(min_length=1, description="What picking this option means.")

    @field_validator("label")
    @classmethod
    def _at_most_five_words(cls, label: str) -> str:
        if len(label.split()) > 5:
            raise ValueError("label must be 1 to 5 words")
        return label


class Question(BaseModel):
    header: str = Field(min_length=1, max_length=12, description="A short chip label, 1 to 12 characters.")
    question: str = Field(min_length=1, description="The full question, ending with '?'.")
    options: list[Option] = Field(
        default_factory=list,
        max_length=4,
        description="No options for a free-text question, otherwise 2 to 4. Never add an 'Other' option.",
    )
    multi_select: bool = Field(default=False, description="Whether the user may pick more than one option.")

    @field_validator("question")
    @classmethod
    def _ends_with_question_mark(cls, question: str) -> str:
        if not question.rstrip().endswith(("?", "？")):
            raise ValueError("question must end with '?'")
        return question

    @field_validator("options")
    @classmethod
    def _zero_or_two_to_four(cls, options: list[Option]) -> list[Option]:
        if len(options) == 1:
            raise ValueError("give no options for a free-text question, or 2 to 4")
        return options


class AskUserQuestionInput(BaseModel):
    questions: Annotated[list[Question], Field(min_length=1, max_length=4)]


def render_questions(payload: dict[str, Any]) -> str:
    """Transport-neutral markdown for a question payload: posted to issues and MRs, and the job API's ``result``."""
    blocks: list[str] = []
    for question in payload["questions"]:
        blocks.append(f"**{question['header']}**: {question['question']}")
        options = question.get("options") or []
        if options:
            blocks.append(
                "\n".join(
                    f"{number}. **{option['label']}**: {option['description']}"
                    for number, option in enumerate(options, start=1)
                )
            )
            if question.get("multi_select"):
                blocks.append(_MULTI_SELECT_HINT)
    blocks.append(_FREE_TEXT_HINT)
    return "\n\n".join(blocks)


def delivered_question_call(messages: Sequence[AnyMessage]) -> ToolCall | None:
    """The ``ask_user_question`` call whose successful delivery is the last message, or ``None``."""
    if len(messages) < 2:
        return None
    caller, delivered = messages[-2], messages[-1]
    if not (
        isinstance(delivered, ToolMessage)
        and delivered.name == ASK_USER_QUESTION_TOOL_NAME
        and delivered.status != "error"
        and delivered.content == QUESTION_DELIVERED
    ):
        return None
    if not isinstance(caller, AIMessage) or len(caller.tool_calls) != 1:
        return None
    call = caller.tool_calls[0]
    if call["name"] != ASK_USER_QUESTION_TOOL_NAME or call["id"] != delivered.tool_call_id:
        return None
    return call


def pending_question(messages: Sequence[AnyMessage]) -> dict[str, Any] | None:
    """The validated payload of the question a thread's last turn ended on, or ``None``."""
    if not messages:
        return None
    close = messages[-1]
    if not isinstance(close, AIMessage) or close.tool_calls:
        return None
    call = delivered_question_call(messages[-3:-1])
    if call is None:
        return None
    return AskUserQuestionInput.model_validate(call["args"]).model_dump()


def is_question_close(messages: Sequence[Any], index: int) -> bool:
    """Whether ``messages[index]`` is the close message a question turn ends on."""
    message = messages[index]
    return (
        index >= 2
        and isinstance(message, AIMessage)
        and not message.tool_calls
        and delivered_question_call(messages[index - 2 : index]) is not None
    )
