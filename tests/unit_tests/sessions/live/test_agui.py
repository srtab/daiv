import pytest
from ag_ui.core.events import EventType
from langchain_core.messages import AIMessageChunk
from sessions.live.agui import RuntimeContextLangGraphAGUIAgent
from sessions.live.event_filter import REASONING_EVENT_TYPES, SubagentEventFilter

from automation.agent.events import ASSISTANT_MESSAGE_EVENT


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
