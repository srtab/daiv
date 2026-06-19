import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware, LoopDetectedError, repeated_tool_streak


def _ai(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _loop(n: int, args: dict | None = None) -> list:
    """A task prompt followed by n identical (AIMessage tool call, ToolMessage result) pairs.

    Mirrors what `awrap_model_call` sees mid-loop: history ends with the last tool RESULT, and the
    model is about to produce call n+1.
    """
    args = args or {"path": "/a", "pattern": "p"}
    messages: list = [HumanMessage(content="task")]
    for i in range(n):
        messages.append(_ai("grep", args, f"c{i}"))
        messages.append(ToolMessage(content="result", tool_call_id=f"c{i}", name="grep"))
    return messages


def _request(messages: list) -> ModelRequest:
    return ModelRequest(model=GenericFakeChatModel(messages=iter([])), messages=messages)


async def _record_handler(seen: list):
    async def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return ModelResponse(result=[AIMessage(content="ok")])

    return handler


# --- repeated_tool_streak ---


def test_streak_counts_identical_consecutive_calls():
    assert repeated_tool_streak(_loop(3)) == 3


def test_streak_ignores_arg_key_order():
    messages = [
        HumanMessage(content="t"),
        _ai("grep", {"path": "/a", "pattern": "p"}, "c0"),
        ToolMessage(content="r", tool_call_id="c0", name="grep"),
        _ai("grep", {"pattern": "p", "path": "/a"}, "c1"),
        ToolMessage(content="r", tool_call_id="c1", name="grep"),
    ]
    assert repeated_tool_streak(messages) == 2


def test_streak_resets_on_different_args():
    messages = _loop(2)
    messages.append(_ai("grep", {"path": "/a", "pattern": "OTHER"}, "cX"))
    messages.append(ToolMessage(content="r", tool_call_id="cX", name="grep"))
    assert repeated_tool_streak(messages) == 1


def test_streak_zero_when_last_message_has_no_tool_calls():
    assert repeated_tool_streak([*_loop(3), AIMessage(content="done")]) == 0


# --- awrap_model_call ---


async def test_passes_through_below_threshold():
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="raise")
    request = _request(_loop(2))
    await mw.awrap_model_call(request, await _record_handler(seen))
    assert seen[0] is request


async def test_injects_ephemeral_reminder_at_threshold():
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="raise")
    request = _request(_loop(3))
    await mw.awrap_model_call(request, await _record_handler(seen))
    assert "system-reminder" in seen[0].messages[-1].content
    assert "grep" in seen[0].messages[-1].content
    # ephemeral: original request still ends with the tool result, no reminder persisted
    assert isinstance(request.messages[-1], ToolMessage)


async def test_raises_at_terminal_streak():
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="raise")
    with pytest.raises(LoopDetectedError):
        await mw.awrap_model_call(_request(_loop(6)), await _record_handler(seen))
    assert seen == []  # model never called


async def test_finalize_ends_loop_without_calling_model():
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="finalize")
    result = await mw.awrap_model_call(_request(_loop(6)), await _record_handler(seen))
    assert isinstance(result, AIMessage)
    assert not result.tool_calls
    assert seen == []


def test_invalid_terminal_rejected():
    with pytest.raises(ValueError):
        LoopBreakerMiddleware(terminal="bogus")
