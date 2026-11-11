import asyncio
import uuid
from dataclasses import dataclass

import pytest
from ag_ui.core import RunAgentInput
from ag_ui.core.events import EventType, RawEvent, ToolCallStartEvent
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.internal_call_transformer import internal_call_metadata
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from sessions.live.agui import RuntimeContextLangGraphAGUIAgent
from sessions.live.event_filter import REASONING_EVENT_TYPES, SubagentEventFilter

from automation.agent.events import (
    ASSISTANT_MESSAGE_EVENT,
    CONTEXT_USAGE_EVENT,
    TOOL_MEDIA_EVENT,
    context_usage_payload,
)
from automation.agent.middlewares.context_usage import ContextUsageMiddleware
from automation.agent.middlewares.tool_media import ToolMediaMiddleware
from automation.agent.usage_tracking import ResolvedWindow
from automation.agent.utils import streamed_assistant_message
from tests.unit_tests.conftest import FakeToolMediaStore

_TEXT_FRAME_TYPES = (EventType.TEXT_MESSAGE_START, EventType.TEXT_MESSAGE_CONTENT, EventType.TEXT_MESSAGE_END)


class _FakeGraph:
    """Minimal stand-in for a CompiledStateGraph. ``nodes`` feeds the subgraph
    scan in ``LangGraphAGUIAgent.__init__``; ``astream_events`` exists only so
    upstream's signature probe sees a ``context`` parameter (newer LangGraph),
    which makes ``get_stream_kwargs`` populate ``context`` from configurable.
    """

    nodes: dict = {}

    async def astream_events(self, _input=None, *, context=None, **kwargs):  # pragma: no cover - never invoked
        yield None


def test_get_stream_kwargs_overrides_configurable_context_with_runtime_ctx():
    """Regression: newer LangGraph's ``astream_events`` accepts ``context``, so
    upstream builds ``context={"thread_id": ...}`` from ``config['configurable']``.
    Our override must replace that dict with the ``RuntimeCtx`` instance — passing
    the dict through makes LangGraph's ``_coerce_context`` call ``RuntimeCtx(**ctx)``,
    raising ``TypeError: RuntimeCtx.__init__() got an unexpected keyword argument 'thread_id'``.
    """
    runtime_ctx = object()  # sentinel; identity is all we assert on
    agent = RuntimeContextLangGraphAGUIAgent(
        name="DAIV", description="d", graph=_FakeGraph(), config={}, runtime_context=runtime_ctx
    )

    kwargs = agent.get_stream_kwargs(
        input={}, config={"configurable": {"thread_id": "abc"}}, subgraphs=False, version="v2"
    )

    assert kwargs["context"] is runtime_ctx


def test_graph_input_replaces_the_stream_input():
    def stream_input(**adapter_kwargs):
        agent = RuntimeContextLangGraphAGUIAgent(
            name="DAIV", description="d", graph=_FakeGraph(), config={}, runtime_context=object(), **adapter_kwargs
        )
        kwargs = agent.get_stream_kwargs(input={"messages": ["ag-ui's"]}, config={}, subgraphs=True, version="v2")
        return kwargs["input"]

    assert stream_input(graph_input={"messages": ["ours"]}) == {"messages": ["ours"]}
    assert stream_input() == {"messages": ["ag-ui's"]}


_METADATA = {"langgraph_checkpoint_ns": "tools:1|model:2", "emit-messages": True}


@pytest.mark.parametrize(
    ("event", "kept_data"),
    [
        (
            RawEvent(
                type=EventType.RAW,
                event={"event": "on_chain_end", "metadata": _METADATA, "data": {"output": {"messages": ["state"]}}},
            ),
            None,
        ),
        (
            RawEvent(
                type=EventType.RAW,
                event={
                    "event": "on_chat_model_end",
                    "metadata": _METADATA,
                    "data": {"input": {"messages": ["prompt"]}, "output": AIMessage(content="", id="a1")},
                },
            ),
            {"output"},
        ),
        (
            ToolCallStartEvent(
                type=EventType.TOOL_CALL_START,
                tool_call_id="c1",
                tool_call_name="ls",
                raw_event={
                    "event": "on_chat_model_stream",
                    "metadata": _METADATA,
                    "data": {"chunk": AIMessageChunk(content="", tool_call_chunks=[{"index": 1, "id": "c1"}])},
                },
            ),
            {"chunk"},
        ),
    ],
    ids=["chain-state", "model-call", "streamed-chunk"],
)
def test_only_what_the_filter_reads_is_dispatched(event, kept_data):
    """The run's state and the model's input are deep-converted on every event otherwise, only to be dropped."""
    agent = RuntimeContextLangGraphAGUIAgent(
        name="DAIV", description="d", graph=_FakeGraph(), config={}, runtime_context=object()
    )
    source = event.event if event.type == EventType.RAW else event.raw_event

    dispatched = agent._dispatch_event(event)

    raw = dispatched.event if dispatched.type == EventType.RAW else dispatched.raw_event
    assert raw["metadata"] == _METADATA
    assert set(raw.get("data", {})) == (kept_data or set())
    assert "data" in source
    if dispatched.type == EventType.TOOL_CALL_START:
        assert SubagentEventFilter._chunk_index(dispatched) == 1


def _assistant_event(**data):
    return {"event": "on_custom_event", "name": ASSISTANT_MESSAGE_EVENT, "data": data}


def test_assistant_message_event_becomes_text_frames():
    """A message the agent produced without a model call (slash command reply, loop-breaker stop)
    streams only through this translation — otherwise it reaches the client only in the terminal
    MESSAGES_SNAPSHOT, which chat-stream.js ignores, and the turn paints empty."""
    frames = RuntimeContextLangGraphAGUIAgent._assistant_message_frames(
        _assistant_event(message_id="m-1", message="### Available Sub-Agents"), {"langgraph_checkpoint_ns": "model:1"}
    )

    assert [f.type for f in frames] == [
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
    ]
    assert {f.message_id for f in frames} == {"m-1"}
    assert frames[0].role == "assistant"
    assert frames[1].delta == "### Available Sub-Agents"
    # Only the provenance keys, not the whole source event: SubagentEventFilter reads the namespace
    # off raw_event to drop a subagent's message, and stamping the event would ship the body twice.
    assert [f.raw_event for f in frames] == [{"metadata": {"langgraph_checkpoint_ns": "model:1"}}] * 3


@pytest.mark.parametrize(
    "event",
    [
        {"event": "on_chat_model_stream", "name": ASSISTANT_MESSAGE_EVENT, "data": {}},
        {"event": "on_custom_event", "name": "some_other_event", "data": {}},
        {"event": "on_custom_event", "name": ASSISTANT_MESSAGE_EVENT, "data": None},
        {"event": "on_custom_event", "name": ASSISTANT_MESSAGE_EVENT, "data": {"message": "no id"}},
        {"event": "on_custom_event", "name": ASSISTANT_MESSAGE_EVENT, "data": {"message_id": "m-1"}},
        "not-a-dict",
    ],
    ids=["wrong-event", "wrong-name", "no-data", "missing-id", "missing-message", "not-a-dict"],
)
def test_assistant_message_frames_ignores_anything_else(event):
    """Every other event flows through untouched; a malformed payload is dropped rather than
    emitting frames the client cannot close."""
    assert RuntimeContextLangGraphAGUIAgent._assistant_message_frames(event, {}) == []


class TestReasoningProvenance:
    """``ag_ui_langgraph`` builds every event type with ``raw_event=event`` except
    the ``REASONING_*`` family, which it emits bare. ``SubagentEventFilter``
    identifies subagent frames solely from ``raw_event.metadata.langgraph_checkpoint_ns``,
    so untagged reasoning reads back as top-level and a subagent's thinking bleeds
    into the parent turn. The agent stamps the source event's metadata onto any
    untagged event so the filter can see where it came from.
    """

    NESTED_NS = "tools:11111111-1111|model:22222222-2222"
    TOP_NS = "model:33333333-3333"

    @staticmethod
    def _agent():
        agent = RuntimeContextLangGraphAGUIAgent(
            name="DAIV", description="d", graph=_FakeGraph(), config={}, runtime_context=object()
        )
        # Mirrors upstream's INITIAL_ACTIVE_RUN (ag_ui_langgraph agent.py), which is a
        # function-local in ``run()`` and so cannot be imported. Only ``id`` is read on
        # these paths today; the rest documents the real runtime shape so a future
        # upstream ``.get()``-to-``[]`` change surfaces as a KeyError, not a silent skip.
        agent.active_run = {
            "id": "run-1",
            "thread_id": "thread-1",
            "mode": "continue",
            "reasoning_process": None,
            "node_name": "model",
            "has_function_streaming": False,
            "streamed_tool_call_ids": set(),
            "model_made_tool_call": False,
            "state_reliable": True,
            "manually_emitted_state": None,
        }
        return agent

    @staticmethod
    def _chunk_event(ns: str, chunk: AIMessageChunk, *, emit_messages: bool | None = None) -> dict:
        """A LangGraph ``on_chat_model_stream`` envelope around ``chunk``."""
        metadata: dict = {"langgraph_checkpoint_ns": ns}
        if emit_messages is not None:
            metadata["emit-messages"] = emit_messages
        return {"event": "on_chat_model_stream", "run_id": "r1", "metadata": metadata, "data": {"chunk": chunk}}

    @classmethod
    def _thinking_event(cls, ns: str, text: str, **kwargs) -> dict:
        """A chunk event carrying an Anthropic extended-thinking block."""
        chunk = AIMessageChunk(content=[{"type": "thinking", "thinking": text, "index": 0}])
        return cls._chunk_event(ns, chunk, **kwargs)

    async def _emit(self, agent, event: dict) -> list:
        return [ev async for ev in agent._handle_single_event(event, {})]

    @staticmethod
    async def _survivors(produced: list) -> list:
        """What the client would actually receive for ``produced``."""

        async def _aiter():
            for ev in produced:
                yield ev

        return [ev async for ev in SubagentEventFilter().apply(_aiter())]

    async def test_reasoning_events_are_stamped_with_source_namespace(self):
        agent = self._agent()
        out = await self._emit(agent, self._thinking_event(self.NESTED_NS, "subagent thought"))

        assert out, "expected the thinking chunk to produce REASONING_* events"
        # Asserting against the production frozenset (not a "REASONING" name prefix)
        # also pins it against the real event stream: a reasoning type upstream adds
        # and REASONING_EVENT_TYPES misses fails here instead of silently un-gating.
        assert all(e.type in REASONING_EVENT_TYPES for e in out)
        for ev in out:
            ns = (ev.raw_event or {}).get("metadata", {}).get("langgraph_checkpoint_ns")
            assert ns == self.NESTED_NS, f"{ev.type.value} lost its namespace: raw_event={ev.raw_event!r}"

    async def test_nested_subagent_thinking_never_reaches_the_client(self):
        """Over the real upstream handler and the real filter, not a mock of the seam.

        Still one layer short of production: ``_handle_stream_events`` (which drives
        this handler) is not exercised, so an upstream change that stops routing
        reasoning through ``_handle_single_event`` would leave this green.
        """
        agent = self._agent()
        produced = []
        for text in ("subagent secret ", "thought"):
            produced += await self._emit(agent, self._thinking_event(self.NESTED_NS, text))

        survivors = await self._survivors(produced)
        leaked = "".join(getattr(ev, "delta", "") for ev in survivors)
        assert survivors == [], f"subagent thinking leaked: {leaked!r}"

    async def test_top_level_thinking_still_reaches_the_client(self):
        """The guard must not silence the main agent's own reasoning."""
        agent = self._agent()
        produced = await self._emit(agent, self._thinking_event(self.TOP_NS, "my own thought"))

        survivors = await self._survivors(produced)
        assert "my own thought" in "".join(getattr(ev, "delta", "") for ev in survivors)

    async def test_stamping_preserves_existing_raw_event(self):
        """Events upstream already tagged (text, tool calls) must pass through untouched."""
        agent = self._agent()
        chunk = AIMessageChunk(content="hello", id="msg-1")
        out = await self._emit(agent, self._chunk_event(self.TOP_NS, chunk))

        assert out, "expected a text chunk to produce events"
        # Upstream sets raw_event to the whole LangGraph event, not just its metadata.
        assert all(ev.raw_event.get("event") == "on_chat_model_stream" for ev in out)


@dataclass
class _Ctx:
    pass


class _ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _run_graph(model, *, content: str, tools=(), middleware=()):
    """The AG-UI event stream of a real ``create_agent`` graph run through the real agent class."""
    agent = create_agent(
        model=model, tools=list(tools), middleware=list(middleware), context_schema=_Ctx, checkpointer=InMemorySaver()
    )
    agui = RuntimeContextLangGraphAGUIAgent(name="DAIV", description="d", graph=agent, runtime_context=_Ctx())
    return agui.run(
        RunAgentInput(
            thread_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            state={},
            messages=[{"id": "m1", "role": "user", "content": content}],
            tools=[],
            context=[],
            forwarded_props={},
        )
    )


async def test_a_middleware_message_reaches_the_stream_as_text_frames():
    """Closes the producer→consumer loop: a real middleware emitting through
    ``streamed_assistant_message``, a real graph, and the real agent class.

    The two halves live in packages with no shared schema and the consumer degrades an
    unreadable payload to "no frames", so unit-testing each side against its own literals
    leaves a renamed key green on both — and back to painting empty turns. This is the only
    test that fails for that.
    """

    class ReplyingMiddleware(AgentMiddleware):
        """Stands in for SlashCommandMiddleware / LoopBreakerMiddleware: answers without the model."""

        async def awrap_model_call(self, request, handler):
            return await streamed_assistant_message("answered without the model")

    stream = _run_graph(
        FakeMessagesListChatModel(responses=[AIMessage(content="the model would have answered")]),
        content="/agents",
        middleware=[ReplyingMiddleware()],
    )

    frames = [e async for e in stream if e is not None and e.type in _TEXT_FRAME_TYPES]

    assert [f.type for f in frames] == [
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
    ]
    assert frames[1].delta == "answered without the model"
    # One message end to end: the id the producer minted is the id the client dedupes replays on.
    assert len({f.message_id for f in frames}) == 1


async def test_every_streamed_tool_call_gets_one_result_even_when_middleware_answers_it():
    """A real graph whose middleware answers one call without running the tool, through the real agent class and
    filter: pins that the tools node's output reaches the hook, which sends the result upstream never emits."""

    @tool
    def edit_file(file_path: str) -> str:
        """Edit a file."""
        return "edited"

    class RejectingMiddleware(AgentMiddleware):
        """Stands in for deepagents' parallel-edit guard."""

        async def awrap_tool_call(self, request, handler):
            if request.tool_call["id"] == "call_2":
                return ToolMessage(content="Error: rejected", tool_call_id="call_2", name="edit_file", status="error")
            return await handler(request)

    calls = [{"name": "edit_file", "args": {"file_path": "/a"}, "id": f"call_{i}"} for i in (1, 2)]
    stream = _run_graph(
        _ToolModel(responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")]),
        content="edit",
        tools=[edit_file],
        middleware=[RejectingMiddleware()],
    )

    events = [e async for e in SubagentEventFilter().apply(stream) if e is not None]

    started = [e.tool_call_id for e in events if e.type == EventType.TOOL_CALL_START]
    results = [(e.tool_call_id, e.content) for e in events if e.type == EventType.TOOL_CALL_RESULT]
    assert sorted(started) == ["call_1", "call_2"]
    assert sorted(results) == [("call_1", "edited"), ("call_2", "Error: rejected")]


_SUMMARY = "the summariser's answer"
_SUMMARY_THOUGHT = "the summariser's thought"


class _ThinkingModel(BaseChatModel):
    """Streams a thought then its answer, yielding between chunks so parallel calls interleave."""

    thought: str = _SUMMARY_THOUGHT
    answer: str = _SUMMARY

    @property
    def _llm_type(self) -> str:
        return "thinking"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for content in ([{"type": "thinking", "thinking": self.thought, "index": 0}], self.answer):
            await asyncio.sleep(0)
            yield ChatGenerationChunk(message=AIMessageChunk(content=content))


async def test_a_marked_model_call_leaves_the_agents_own_stream_intact():
    """Compaction marks its summary call internal and makes it in the agent's own model node, just before the agent:
    its open thought must not carry the agent's next one under a message the client never saw start."""

    class CompactingMiddleware(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            await _ThinkingModel().ainvoke("compact", config={"metadata": internal_call_metadata()})
            return await handler(request)

    stream = _run_graph(
        _ThinkingModel(thought="my own thought", answer="done"), content="hi", middleware=[CompactingMiddleware()]
    )

    events = [e async for e in SubagentEventFilter().apply(stream) if e is not None]

    started = {e.message_id for e in events if e.type == EventType.REASONING_MESSAGE_START}
    thoughts = [e for e in events if e.type == EventType.REASONING_MESSAGE_CONTENT]
    assert [e.delta for e in thoughts] == ["my own thought"]
    assert thoughts[0].message_id in started
    assert [e.delta for e in events if e.type == EventType.TEXT_MESSAGE_CONTENT] == ["done"]


async def test_parallel_model_calls_made_inside_tools_never_reach_the_chat():
    """web_fetch's summariser runs under the agent's event stream, unmarked: being inside a tool is enough."""

    @tool
    async def fetch_page() -> str:
        """Fetch a page."""
        return (await _ThinkingModel().ainvoke("summarise")).text

    calls = [{"name": "fetch_page", "args": {}, "id": f"call_{i}"} for i in (1, 2)]
    stream = _run_graph(
        _ToolModel(responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")]),
        content="fetch",
        tools=[fetch_page],
    )

    events = [e async for e in SubagentEventFilter().apply(stream) if e is not None]

    results = [(e.tool_call_id, e.content) for e in events if e.type == EventType.TOOL_CALL_RESULT]
    assert sorted(results) == [("call_1", _SUMMARY), ("call_2", _SUMMARY)]
    deltas = [e.delta for e in events if getattr(e, "delta", None)]
    assert _SUMMARY not in deltas
    assert _SUMMARY_THOUGHT not in deltas
    assert not [e for e in events if e.type in REASONING_EVENT_TYPES]


def _tools_node_end(*messages: ToolMessage) -> dict:
    return {"event": "on_chain_end", "name": "tools", "data": {"output": {"messages": list(messages)}}}


def _hook_agent() -> RuntimeContextLangGraphAGUIAgent:
    return RuntimeContextLangGraphAGUIAgent(
        name="DAIV", description="d", graph=_FakeGraph(), config={}, runtime_context=object()
    )


def test_an_unanswered_error_result_is_marked_failed_for_the_ui():
    rejected = ToolMessage(content="Budget exhausted.", tool_call_id="c1", name="bash", status="error", id="r1")
    provenance = {"langgraph_checkpoint_ns": "tools:1"}

    [result] = _hook_agent()._unanswered_tool_results(_tools_node_end(rejected), provenance)

    assert (result.tool_call_id, result.message_id, result.content) == ("c1", "r1", "Error: Budget exhausted.")
    assert result.raw_event == {"metadata": provenance}


def test_a_tool_call_is_answered_only_once():
    agent = _hook_agent()
    message = ToolMessage(content="ok", tool_call_id="c1", name="ls")

    assert len(agent._unanswered_tool_results(_tools_node_end(message), {})) == 1
    assert agent._unanswered_tool_results(_tools_node_end(message), {}) == []


async def test_a_model_call_reaches_the_stream_as_a_context_usage_frame():
    """The real middleware in a real graph, through the real agent class: pins that the
    dispatch survives AG-UI translation as a CUSTOM frame and that the producer goes through
    the builder — a hand-rolled payload dict in the middleware fails here. Renames of the
    shared constants are caught by the node test (the JS side hand-types the literals)."""

    reply = AIMessage(
        content="done",
        usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        response_metadata={"model_name": "anthropic/claude-sonnet-4.6"},
    )
    stream = _run_graph(
        FakeMessagesListChatModel(responses=[reply]), content="hi", middleware=[ContextUsageMiddleware()]
    )

    frames = [
        e
        async for e in stream
        if e is not None and e.type == EventType.CUSTOM and getattr(e, "name", "") == CONTEXT_USAGE_EVENT
    ]

    assert len(frames) == 1
    assert frames[0].value == context_usage_payload(
        model="anthropic/claude-sonnet-4.6",
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        window=ResolvedWindow(1_000_000, "genai_prices"),
    )


_IMAGE = {"type": "image", "base64": "iVBORw0K", "mime_type": "image/png"}


async def test_a_screenshot_streams_as_text_then_a_media_event():
    """A real graph whose tool returns text and an image, through the real agent class and filter: the result frame
    carries only the text, and the media follows in its own frame with the endpoint URL."""

    class _ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool
    def screenshot() -> list:
        """Take a screenshot."""
        return [{"type": "text", "text": "taken"}, _IMAGE]

    stream = _run_graph(
        _ToolModel(
            responses=[
                AIMessage(content="", tool_calls=[{"name": "screenshot", "args": {}, "id": "call_1"}]),
                AIMessage(content="done"),
            ]
        ),
        content="shoot",
        tools=[screenshot],
        middleware=[ToolMediaMiddleware(store=FakeToolMediaStore())],
    )

    events = [e async for e in SubagentEventFilter().apply(stream) if e is not None]

    results = [e for e in events if e.type == EventType.TOOL_CALL_RESULT]
    media = [e for e in events if e.type == EventType.CUSTOM and e.name == TOOL_MEDIA_EVENT]
    assert [r.content for r in results] == ["taken"]
    assert media[0].value["tool_call_id"] == results[0].tool_call_id
    assert media[0].value["media"][0]["url"].endswith(f"/tool-media/{uuid.UUID(int=1)}/")
    assert events.index(results[0]) < events.index(media[0])


def test_an_unanswered_result_with_media_is_text_only():
    message = ToolMessage(content=[{"type": "text", "text": "x"}, _IMAGE], tool_call_id="c1", name="shot")

    [result] = _hook_agent()._unanswered_tool_results(_tools_node_end(message), {})

    assert result.content == "x"


def test_an_unanswered_text_only_result_keeps_upstream_content():
    message = ToolMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], tool_call_id="c1")

    [result] = _hook_agent()._unanswered_tool_results(_tools_node_end(message), {})

    assert result.content == "ab"
