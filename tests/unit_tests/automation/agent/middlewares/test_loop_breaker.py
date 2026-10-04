import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from automation.agent.events import ASSISTANT_MESSAGE_EVENT
from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware, repeated_tool_streak
from automation.agent.synthetic import is_synthetic, synthetic_message


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
async def test_injects_and_saves_reminder_at_threshold(streak: int):
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="error")
    request = _request(_loop(streak))
    response = await mw.awrap_model_call(request, await _record_handler(seen))
    reminder = seen[0].messages[-1]
    assert "system-reminder" in reminder.content
    assert "grep" in reminder.content
    # countdown decreases as streak increases: at streak 3 → 3 left, streak 4 → 2 left, streak 5 → 1 left
    expected_remaining = mw._terminal_streak - streak
    assert f"repeat it {expected_remaining} more time(s)" in reminder.content
    assert response.result[0] is reminder
    assert is_synthetic(reminder)
    assert isinstance(request.messages[-1], ToolMessage)


async def test_error_at_terminal_streak_returns_aimessage_without_calling_model():
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="error")
    result = await mw.awrap_model_call(_request(_loop(6)), await _record_handler(seen))
    assert isinstance(result, AIMessage)
    assert not result.tool_calls
    assert seen == []  # model never called
    # result must unambiguously signal a failure, not "no findings". The `ERROR:` *prefix* is a
    # contract, not decoration: the code-review orchestrator (SKILL.md Step 5) classifies a detector
    # result as failed by testing whether it opens with `ERROR:`, and counts the dimension as
    # uncovered. Rewording this into "stopped with an ERROR after…" would keep a substring check
    # green while silently making every loop-stopped detector read as a clean pass.
    assert result.content.startswith("ERROR:")
    assert "did NOT complete" in result.content
    assert "no findings" in result.content


async def test_finalize_ends_loop_without_calling_model(emit_custom_event):
    seen: list = []
    mw = LoopBreakerMiddleware(terminal="finalize")
    result = await mw.awrap_model_call(_request(_loop(6)), await _record_handler(seen))
    assert isinstance(result, AIMessage)
    assert not result.tool_calls
    assert seen == []
    assert "ERROR" not in result.content


async def test_finalize_message_is_streamed_to_the_chat(emit_custom_event):
    """This message replaces the model's turn, so nothing else emits text frames for it — without
    the event the user's turn ends in silence."""
    mw = LoopBreakerMiddleware(terminal="finalize")
    result = await mw.awrap_model_call(_request(_loop(6)), await _record_handler([]))

    assert emit_custom_event.await_args.args[0] == ASSISTANT_MESSAGE_EVENT
    assert emit_custom_event.await_args.args[1] == {"message_id": result.id, "message": result.content}


async def test_error_terminal_message_is_not_streamed(emit_custom_event):
    """The ``error`` terminal is a sentinel the code-review orchestrator parses by its ``ERROR:``
    prefix, not prose for a human, so it is deliberately left unstreamed."""
    mw = LoopBreakerMiddleware(terminal="error")
    await mw.awrap_model_call(_request(_loop(6)), await _record_handler([]))

    emit_custom_event.assert_not_awaited()


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


def _reminder() -> HumanMessage:
    return synthetic_message("<system-reminder>stop</system-reminder>", kind="loop_breaker")


def test_streak_counts_across_saved_reminders():
    args = {"path": "/a", "pattern": "p"}
    messages = [*_loop(3), _reminder(), _ai("grep", args, "c3"), ToolMessage(content="r", tool_call_id="c3")]
    assert repeated_tool_streak(messages) == 4


def test_streak_stops_at_a_real_human_message_after_a_reminder():
    args = {"path": "/a", "pattern": "p"}
    messages = [
        *_loop(3),
        _reminder(),
        _ai("grep", args, "c3"),
        ToolMessage(content="r", tool_call_id="c3"),
        HumanMessage(content="continue"),
        _ai("grep", args, "c4"),
        ToolMessage(content="r", tool_call_id="c4"),
    ]
    assert repeated_tool_streak(messages) == 1


class _ToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _RequestProbe(AgentMiddleware):
    """Innermost middleware: records each request exactly as the model receives it."""

    def __init__(self):
        super().__init__()
        self.requests: list[list] = []

    async def awrap_model_call(self, request, handler):
        self.requests.append(list(request.messages))
        return await handler(request)


@tool("grep")
def _grep_tool(pattern: str) -> str:
    """Search the repository."""
    return "result"


def _shape(messages: list) -> list[tuple[str, object]]:
    return [(type(m).__name__, m.content) for m in messages]


async def test_saved_reminders_keep_every_request_a_prefix_of_the_next():
    calls = [_ai("grep", {"pattern": "p"}, f"c{i}") for i in range(6)]
    probe = _RequestProbe()
    agent = create_agent(
        model=_ToolModel(messages=iter(calls)),
        tools=[_grep_tool],
        middleware=[LoopBreakerMiddleware(terminal="error"), probe],
    )

    result = await agent.ainvoke({"messages": [HumanMessage(content="find it")]})

    for earlier, later in zip(probe.requests, probe.requests[1:], strict=False):
        assert _shape(later)[: len(earlier)] == _shape(earlier)
    messages = result["messages"]
    reminders = [i for i, m in enumerate(messages) if is_synthetic(m)]
    assert len(reminders) == 3
    assert all(isinstance(messages[i + 1], AIMessage) for i in reminders)
    assert messages[-1].content.startswith("ERROR:")


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
