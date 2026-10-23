import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from ag_ui.core.events import EventType, RawEvent, RunErrorEvent, TextMessageContentEvent
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import AsyncCallbackHandler, AsyncCallbackManager
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langsmith.run_helpers import tracing_context
from langsmith.run_trees import RunTree
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


def _failing_agent(error: BaseException):
    class FailingModel(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            raise error
            yield

        async def _agenerate(self, *args, **kwargs):
            raise error

    return _agent(model=FailingModel(messages=iter([])))


class _NetworkRedis(FakeAsyncRedis):
    """Writes wait on the network as Redis's do, so other tasks run while the relay publishes."""

    async def delete(self, *keys):
        await asyncio.sleep(0.005)
        return await super().delete(*keys)

    def pipeline(self, transaction=True):
        pipeline = super().pipeline(transaction)
        execute = pipeline.execute

        async def _execute():
            await asyncio.sleep(0.005)
            return await execute()

        pipeline.execute = _execute
        return pipeline


def _two_tool_rounds(calls: list[str]):
    @tool
    async def slow() -> str:
        """Take a while."""
        await asyncio.sleep(0.05)
        calls.append("ran")
        return "ok"

    model = _ToolModel(
        messages=iter([
            AIMessage(content="", id="a1", tool_calls=[{"name": "slow", "args": {}, "id": "c1"}]),
            AIMessage(content="", id="a2", tool_calls=[{"name": "slow", "args": {}, "id": "c2"}]),
            AIMessage(content="done", id="a3"),
        ]),
        disable_streaming=True,
    )
    return create_agent(model=model, tools=[slow], checkpointer=InMemorySaver(), context_schema=_Ctx)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _relay(thread_id: str = "t-1", client: FakeAsyncRedis | None = None) -> RunRelay:
    return RunRelay(thread_id, "h-1", client=client or FakeAsyncRedis())


async def _live(agent, messages, run_relay: RunRelay, **config) -> dict:
    return await ainvoke_live(
        agent,
        {"messages": messages},
        config=_config(run_relay.thread_id) | config,
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


async def test_publish_event_drops_raw_events():
    run_relay = _relay()

    await publish_event(run_relay, RawEvent(type=EventType.RAW, event={"event": "on_chat_model_start"}))

    assert await _entries(run_relay) == []


async def test_publish_event_leaves_the_raw_copy_off_the_wire():
    run_relay = _relay()
    raw = {"event": "on_chat_model_stream", "metadata": {"langgraph_checkpoint_ns": ""}}
    event = TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id="m1", delta="hi", raw_event=raw)

    await publish_event(run_relay, event)

    [payload] = _events(await _entries(run_relay))
    assert "rawEvent" not in payload
    assert payload["delta"] == "hi"
    assert event.raw_event == raw


async def test_the_model_input_never_reaches_the_relay():
    agent = create_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="hello there")])),
        tools=[],
        system_prompt="SYSTEM-PROMPT-MARKER",
        checkpointer=InMemorySaver(),
        context_schema=_Ctx,
    )
    run_relay = _relay()

    await _live(agent, [HumanMessage(content="hi")], run_relay)

    published = [entry.data for entry in await _entries(run_relay) if entry.data]
    assert published
    assert not any("SYSTEM-PROMPT-MARKER" in data or '"RAW"' in data or "rawEvent" in data for data in published)


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
    run_relay = _relay()

    with pytest.raises(RuntimeError) as raised:
        await _live(_failing_agent(boom), [HumanMessage(content="hi")], run_relay)

    assert raised.value is boom
    entries = await _entries(run_relay)
    errors = [e for e in _events(entries) if e["type"] == "RUN_ERROR"]
    assert [(e["message"], e["code"]) for e in errors] == [(RUN_FAILED_MESSAGE, "run_failed")]
    assert not any("secret" in entry.data for entry in entries if entry.data)
    assert entries[-1].is_end


@contextlib.asynccontextmanager
async def _nested_under_a_callers_run(caller: str):
    """The config extras for a graph run nested under a caller's run: a LangSmith trace, or a parent's callbacks."""
    if caller == "langsmith":
        with tracing_context(parent=RunTree(name="caller"), enabled=False):
            yield {}
    else:
        run_manager = await AsyncCallbackManager(handlers=[]).on_chain_start({}, {}, run_id=uuid.uuid4())
        yield {"callbacks": run_manager.get_child()}


@pytest.mark.parametrize("caller", ["langsmith", "callbacks"])
async def test_a_run_nested_under_a_callers_run_returns_its_output(caller):
    async with _nested_under_a_callers_run(caller) as config:
        result = await _live(_agent(AIMessage(content="nested")), [HumanMessage(content="hi")], _relay(), **config)

    assert result["messages"][-1].content == "nested"


@pytest.mark.parametrize("caller", ["langsmith", "callbacks"])
async def test_a_run_nested_under_a_callers_run_raises_its_graph_error(caller):
    boom = RuntimeError("boom")

    async with _nested_under_a_callers_run(caller) as config:
        with pytest.raises(RuntimeError) as raised:
            await _live(_failing_agent(boom), [HumanMessage(content="hi")], _relay(), **config)

    assert raised.value is boom


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
    """Only the run started with the call's own run id is the root: a subagent that raises inside a ``task`` call the
    parent recovers from is such an inner failure."""

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


@pytest.mark.parametrize("log_kept", [True, False], ids=["log-kept", "log-dropped"])
async def test_a_translation_failure_mid_run_still_returns_the_graphs_result(log_kept):
    """ag-ui abandons the graph's stream when translating one of its events raises. A kept log record of that keeps
    the stream alive and the graph runs on, orphaned; a dropped one lets it be finalized, which cancels the graph."""
    calls: list[str] = []
    agent = _two_tool_rounds(calls)
    translate = RuntimeContextLangGraphAGUIAgent._handle_single_event

    async def failing_at_the_first_tool(self, event, state):
        if event.get("event") == "on_tool_start":
            raise ValueError("adapter bug")
        async for agui_event in translate(self, event, state):
            yield agui_event

    with (
        patch.object(RuntimeContextLangGraphAGUIAgent, "_handle_single_event", failing_at_the_first_tool),
        patch.object(logging.getLogger("ag_ui_langgraph.agent"), "disabled", not log_kept),
    ):
        result = await _live(agent, [HumanMessage(content="hi")], _relay(client=_NetworkRedis()))
    checkpointed = (await agent.aget_state(_config("t-1"))).values["messages"]
    await asyncio.sleep(0.2)

    assert result["messages"][-1].content == "done"
    assert calls == ["ran", "ran"]
    assert (await agent.aget_state(_config("t-1"))).values["messages"] == checkpointed


async def test_a_cancelled_call_leaves_no_graph_running():
    tool_started, publish_stalled, tool_ends = asyncio.Event(), asyncio.Event(), []

    @tool
    async def slow() -> str:
        """Take a while."""
        tool_started.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            tool_ends.append("cancelled")
            raise
        tool_ends.append("finished")
        return "ok"

    model = _ToolModel(
        messages=iter([
            AIMessage(content="", id="a1", tool_calls=[{"name": "slow", "args": {}, "id": "c1"}]),
            AIMessage(content="done", id="a2"),
        ]),
        disable_streaming=True,
    )
    agent = create_agent(model=model, tools=[slow], checkpointer=InMemorySaver(), context_schema=_Ctx)

    class StallingRedis(FakeAsyncRedis):
        """Publishing the tool call hangs while the graph runs on into the tool."""

        async def xadd(self, key, fields, *args, **kwargs):
            if "TOOL_CALL_START" in fields.get("data", "") and not publish_stalled.is_set():
                publish_stalled.set()
                await asyncio.sleep(10)
            return await super().xadd(key, fields, *args, **kwargs)

    call = asyncio.create_task(_live(agent, [HumanMessage(content="hi")], _relay(client=StallingRedis())))
    await tool_started.wait()
    await publish_stalled.wait()
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call

    assert tool_ends == ["cancelled"]


async def test_a_root_error_that_is_not_an_exception_raises_live_run_error():
    """Something other than the caller cancels the graph's run, as finalizing an abandoned stream does."""

    class GraphRunTask(AsyncCallbackHandler):
        run_inline = True
        task: asyncio.Task | None = None

        async def on_chain_start(self, *args, **kwargs):
            self.task = self.task or asyncio.current_task()

    graph_run = GraphRunTask()

    class CancelsTheGraphRun(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            graph_run.task.cancel()
            await asyncio.sleep(1)
            return await handler(request)

    agent = create_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="never")])),
        tools=[],
        middleware=[CancelsTheGraphRun()],
        checkpointer=InMemorySaver(),
        context_schema=_Ctx,
    )

    with pytest.raises(LiveRunError) as raised:
        await _live(agent, [HumanMessage(content="hi")], _relay(), callbacks=[graph_run])

    assert isinstance(raised.value.__cause__, asyncio.CancelledError)


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
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="boom"):
        await _live(_failing_agent(RuntimeError("boom")), [HumanMessage(content="hi")], _relay())

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
