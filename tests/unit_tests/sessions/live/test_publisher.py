import contextlib
import json
import logging
import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from ag_ui.core.events import EventType, RunErrorEvent
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from redis.exceptions import ConnectionError as RedisConnectionError
from sessions.live.agui import RuntimeContextLangGraphAGUIAgent
from sessions.live.publisher import LiveRunError, _driving_live_run, ainvoke_live, publish_event
from sessions.live.relay import RunRelay

from core.constants import RUN_FAILED_MESSAGE
from tests.unit_tests.conftest import FakeAsyncRedis, _FakePipeline


@dataclass(frozen=True)
class _Ctx:
    repo_id: str = "repo"


class _ToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _agent(*replies: AIMessage, model: GenericFakeChatModel | None = None):
    return create_agent(
        model=model or GenericFakeChatModel(messages=iter(replies)),
        tools=[],
        checkpointer=InMemorySaver(),
        context_schema=_Ctx,
    )


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _relay(thread_id: str = "t-1", client: FakeAsyncRedis | None = None) -> RunRelay:
    return RunRelay(thread_id, "h-1", client=client or FakeAsyncRedis())


async def _live(agent, messages, run_relay: RunRelay) -> dict:
    return await ainvoke_live(
        agent,
        {"messages": messages},
        config=_config(run_relay.thread_id),
        context=_Ctx(),
        recursion_limit=50,
        run_relay=run_relay,
    )


async def _entries(run_relay: RunRelay):
    return await run_relay.read_events("0-0", block_ms=0, count=10_000)


def _events(entries) -> list[dict]:
    return [json.loads(entry.data) for entry in entries if not entry.is_end]


async def test_publish_event_stamps_the_emit_time():
    run_relay = _relay()
    before = int(time.time() * 1000)

    await publish_event(run_relay, RunErrorEvent(type=EventType.RUN_ERROR, message="m"))

    [payload] = _events(await _entries(run_relay))
    assert isinstance(payload["timestamp"], int)
    assert before <= payload["timestamp"] <= int(time.time() * 1000)
    assert payload == {"type": "RUN_ERROR", "message": "m", "timestamp": payload["timestamp"]}


async def test_it_returns_what_ainvoke_returns():
    script = [AIMessage(content="hello there")]
    plain = await _agent(*script).ainvoke(
        {"messages": [HumanMessage(content="hi")]}, config=_config("t-plain"), context=_Ctx()
    )

    live = await _live(_agent(*script), [HumanMessage(content="hi")], _relay("t-live"))

    assert [m.content for m in live["messages"]] == [m.content for m in plain["messages"]]


async def test_it_streams_the_reply_token_by_token_to_the_relay():
    run_relay = _relay()

    await _live(_agent(AIMessage(content="hello there")), [HumanMessage(content="hi")], run_relay)

    entries = await _entries(run_relay)
    deltas = [e["delta"] for e in _events(entries) if e["type"] == "TEXT_MESSAGE_CONTENT"]
    assert len(deltas) >= 2
    assert "".join(deltas) == "hello there"
    assert entries[-1].is_end


async def test_input_ids_already_in_the_checkpoint_append_like_ainvoke():
    script = [AIMessage(content="first", id="a1"), AIMessage(content="second", id="a2")]
    seed = {"messages": [HumanMessage(id="issue-ctx", content="ctx")]}
    resend = [HumanMessage(id="issue-ctx", content="ctx"), HumanMessage(id="n2", content="next")]
    live_agent, twin_agent = _agent(*script), _agent(*script)
    await live_agent.ainvoke(seed, config=_config("t-live"), context=_Ctx())
    await twin_agent.ainvoke(seed, config=_config("t-twin"), context=_Ctx())

    live = await _live(live_agent, resend, _relay("t-live"))
    twin = await twin_agent.ainvoke({"messages": resend}, config=_config("t-twin"), context=_Ctx())

    assert [(m.type, m.id) for m in live["messages"]] == [(m.type, m.id) for m in twin["messages"]]
    assert ("ai", "a1") in [(m.type, m.id) for m in live["messages"]]


async def test_a_checkpointed_last_message_never_forks_the_thread():
    """ag-ui would replay from before ``c1``, dropping ``a1``: a mention re-sent after the issue was edited."""
    script = [AIMessage(content="first", id="a1"), AIMessage(content="second", id="a2")]
    seed = {"messages": [HumanMessage(id="ctx-1", content="issue v1"), HumanMessage(id="c1", content="fix it")]}
    resend = [HumanMessage(id="ctx-2", content="issue v2"), HumanMessage(id="c1", content="fix it")]
    live_agent, twin_agent = _agent(*script), _agent(*script)
    await live_agent.ainvoke(seed, config=_config("t-live"), context=_Ctx())
    await twin_agent.ainvoke(seed, config=_config("t-twin"), context=_Ctx())

    live = await _live(live_agent, resend, _relay("t-live"))
    twin = await twin_agent.ainvoke({"messages": resend}, config=_config("t-twin"), context=_Ctx())

    assert [(m.type, m.id) for m in live["messages"]] == [(m.type, m.id) for m in twin["messages"]]
    assert ("ai", "a1") in [(m.type, m.id) for m in live["messages"]]


async def test_a_graph_error_raises_the_original_exception():
    boom = RuntimeError("secret /srv/x")

    class FailingModel(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            raise boom
            yield

        async def _agenerate(self, *args, **kwargs):
            raise boom

    run_relay = _relay()

    with pytest.raises(RuntimeError) as raised:
        await _live(_agent(model=FailingModel(messages=iter([]))), [HumanMessage(content="hi")], run_relay)

    assert raised.value is boom
    entries = await _entries(run_relay)
    errors = [e for e in _events(entries) if e["type"] == "RUN_ERROR"]
    assert [(e["message"], e["code"]) for e in errors] == [(RUN_FAILED_MESSAGE, "run_failed")]
    assert not any("secret" in entry.data for entry in entries if entry.data)
    assert entries[-1].is_end


async def test_a_failure_before_the_graph_runs_raises_live_run_error():
    agent = _agent(AIMessage(content="never"))
    run_relay = _relay()

    with (
        patch.object(agent, "aget_state", side_effect=RuntimeError("no checkpoint")),
        pytest.raises(LiveRunError, match="no checkpoint"),
    ):
        await _live(agent, [HumanMessage(content="hi")], run_relay)

    errors = [e for e in _events(await _entries(run_relay)) if e["type"] == "RUN_ERROR"]
    assert [e["message"] for e in errors] == [RUN_FAILED_MESSAGE]


async def test_an_inner_failure_the_graph_recovers_from_does_not_fail_the_run():
    """A subagent that raises inside a ``task`` call the parent recovers from is such a failure."""

    def fail(_):
        raise RuntimeError("inner")

    @tool
    async def probe() -> str:
        """Run a step that fails, and recover."""
        with contextlib.suppress(RuntimeError):
            await RunnableLambda(fail).ainvoke(None)
        return "recovered"

    model = _ToolModel(
        messages=iter([
            AIMessage(content="", id="a1", tool_calls=[{"name": "probe", "args": {}, "id": "call-1"}]),
            AIMessage(content="done", id="a2"),
        ]),
        disable_streaming=True,
    )
    agent = create_agent(model=model, tools=[probe], checkpointer=InMemorySaver(), context_schema=_Ctx)

    result = await _live(agent, [HumanMessage(content="hi")], _relay())

    assert [m.content for m in result["messages"][-2:]] == ["recovered", "done"]


async def test_a_dead_relay_never_fails_the_run(caplog):
    with (
        patch.object(_FakePipeline, "execute", side_effect=RedisConnectionError("down")),
        caplog.at_level("ERROR", logger="daiv.sessions"),
    ):
        result = await _live(_agent(AIMessage(content="still here")), [HumanMessage(content="hi")], _relay())

    assert result["messages"][-1].content == "still here"
    assert len([r for r in caplog.records if r.name == "daiv.sessions" and r.levelname == "ERROR"]) == 1


async def test_a_reused_holder_id_starts_a_clean_stream():
    run_relay = _relay()
    await run_relay.publish_event('{"type":"RUN_FINISHED"}')
    await run_relay.publish_end()

    await _live(_agent(AIMessage(content="again")), [HumanMessage(content="hi")], run_relay)

    entries = await _entries(run_relay)
    assert not entries[0].is_end
    assert [entry.is_end for entry in entries].count(True) == 1


async def test_ag_ui_does_not_report_a_failure_the_executor_reraises(caplog):
    class FailingModel(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            raise RuntimeError("boom")
            yield

        async def _agenerate(self, *args, **kwargs):
            raise RuntimeError("boom")

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="boom"):
        await _live(_agent(model=FailingModel(messages=iter([]))), [HumanMessage(content="hi")], _relay())

    assert [r for r in caplog.records if r.name == "ag_ui_langgraph.agent" and r.levelname == "ERROR"] == []


def test_ag_ui_failure_log_still_reaches_chat(caplog):
    with caplog.at_level("ERROR"):
        logging.getLogger("ag_ui_langgraph.agent").error("LangGraph run failed")

    assert [r.name for r in caplog.records] == ["ag_ui_langgraph.agent"]


async def test_ag_ui_reports_its_own_failure_while_driving_a_run(caplog):
    async def broken(self, event, state):
        raise ValueError("adapter bug")
        yield

    with (
        patch.object(RuntimeContextLangGraphAGUIAgent, "_handle_single_event", broken),
        caplog.at_level("ERROR"),
        contextlib.suppress(Exception),
    ):
        await _live(_agent(AIMessage(content="fine")), [HumanMessage(content="hi")], _relay())

    assert [r.getMessage() for r in caplog.records if r.name == "ag_ui_langgraph.agent"] == ["LangGraph run failed"]


async def test_the_failure_filter_stops_applying_once_the_run_ends():
    await _live(_agent(AIMessage(content="ok")), [HumanMessage(content="hi")], _relay())

    assert _driving_live_run.get() is None
