import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware, repeated_tool_streak


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


def test_streak_zero_on_empty_and_human_only_messages():
    assert repeated_tool_streak([]) == 0
    assert repeated_tool_streak([HumanMessage(content="t")]) == 0


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
    mw = LoopBreakerMiddleware(terminal="error")
    request = _request(_loop(2))
    await mw.awrap_model_call(request, await _record_handler(seen))
    assert seen[0] is request


@pytest.mark.parametrize("streak", [3, 4, 5])
async def test_injects_ephemeral_reminder_at_threshold(streak: int):
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="error")
    request = _request(_loop(streak))
    await mw.awrap_model_call(request, await _record_handler(seen))
    reminder_content = seen[0].messages[-1].content
    assert "system-reminder" in reminder_content
    assert "grep" in reminder_content
    # countdown decreases as streak increases: at streak 3 → 3 left, streak 4 → 2 left, streak 5 → 1 left
    expected_remaining = mw._terminal_streak - streak
    assert f"repeat it {expected_remaining} more time(s)" in reminder_content
    # ephemeral: original request still ends with the tool result, no reminder persisted
    assert isinstance(request.messages[-1], ToolMessage)


async def test_error_at_terminal_streak_returns_aimessage_without_calling_model():
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="error")
    result = await mw.awrap_model_call(_request(_loop(6)), await _record_handler(seen))
    assert isinstance(result, AIMessage)
    assert not result.tool_calls
    assert seen == []  # model never called
    # result must unambiguously signal a failure, not "no findings"
    assert "ERROR" in result.content
    assert "did NOT complete" in result.content
    assert "no findings" in result.content


async def test_finalize_ends_loop_without_calling_model():
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="finalize")
    result = await mw.awrap_model_call(_request(_loop(6)), await _record_handler(seen))
    assert isinstance(result, AIMessage)
    assert not result.tool_calls
    assert seen == []
    assert "ERROR" not in result.content


def test_streak_stops_at_midconversation_human_message():
    """A HumanMessage mid-history is a turn boundary: the streak resets to the trailing run only."""
    args = {"path": "/a", "pattern": "p"}
    messages: list = [
        HumanMessage(content="task"),
        _ai("grep", args, "c0"),
        ToolMessage(content="result", tool_call_id="c0", name="grep"),
        _ai("grep", args, "c1"),
        ToolMessage(content="result", tool_call_id="c1", name="grep"),
        HumanMessage(content="continue"),
        _ai("grep", args, "c2"),
        ToolMessage(content="result", tool_call_id="c2", name="grep"),
        _ai("grep", args, "c3"),
        ToolMessage(content="result", tool_call_id="c3", name="grep"),
    ]
    assert repeated_tool_streak(messages) == 2


def test_invalid_terminal_rejected():
    with pytest.raises(ValueError):
        LoopBreakerMiddleware(terminal="bogus")


def test_invalid_terminal_raise_rejected():
    # "raise" was the old value — must now be rejected
    with pytest.raises(ValueError):
        LoopBreakerMiddleware(terminal="raise")


def test_repeat_threshold_below_one_rejected():
    with pytest.raises(ValueError, match="repeat_threshold must be >= 1"):
        LoopBreakerMiddleware(terminal="error", repeat_threshold=0)


def test_max_reminders_below_zero_rejected():
    with pytest.raises(ValueError, match="max_reminders must be >= 0"):
        LoopBreakerMiddleware(terminal="error", max_reminders=-1)
