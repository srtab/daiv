from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ag_ui.core.events import EventType, ToolCallArgsEvent, ToolCallEndEvent, ToolCallStartEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from ag_ui.core.events import BaseEvent

# ``ag_ui_langgraph`` honors ``emit-messages: False`` for text frames only: the
# reasoning branch returns before upstream reaches that gate, so a subagent
# configured to stay silent still streams its thinking. The filter applies the
# same gate to reasoning.
#
# Scoping it to reasoning is upstream's own taxonomy, not a DAIV carve-out:
# upstream splits the flag in two (``emit-messages`` and ``emit-tool-calls``), so
# "messages ⊃ text + reasoning" while tool calls are a separate axis. Extending
# this gate to TOOL_CALL_* would also break the publish pipeline, which runs under
# ``emit-messages: False`` and relies on its tool frames reaching the chat as
# progress chips ("Creating merge request…").
REASONING_EVENT_TYPES = frozenset({
    EventType.REASONING_START,
    EventType.REASONING_MESSAGE_START,
    EventType.REASONING_MESSAGE_CONTENT,
    EventType.REASONING_MESSAGE_CHUNK,
    EventType.REASONING_MESSAGE_END,
    EventType.REASONING_END,
    EventType.REASONING_ENCRYPTED_VALUE,
})


class SubagentEventFilter:
    """Reorder/suppress AGUI events so subagent frames don't leak into the parent turn,
    and fix ag_ui_langgraph's mis-routing of streamed args for parallel tool_calls.

    Three upstream behaviors collide on multi-tool_call turns:

    1. ag_ui_langgraph drops the parent's TOOL_CALL_START on the
       text→tool_call transition chunk (the chunk that ends the parent's text
       stream also carries the new tool_call name, but the handler returns
       after emitting TEXT_MESSAGE_END). Subsequent chunks only have args, so
       OnChatModelStream never reaches ``is_tool_call_start_event``. The
       TOOL_CALL_START finally arrives from the OnToolEnd re-emit — *after*
       the tool (and any subagent it dispatched) has already produced output.
       Originally observed on ``task`` calls; affects any tool that follows
       text in an AIMessage.

    2. With ``stream_subgraphs=True``, every chunk emitted from inside
       ``subagent.ainvoke()`` flows through the parent's stream with a
       nested ``langgraph_checkpoint_ns`` (``"tools:UUID|model:UUID"``).
       ``REASONING_*`` events arrive with no namespace of their own; see
       ``_metadata`` and ``RuntimeContextLangGraphAGUIAgent._handle_single_event``.

    3. When the LLM emits parallel tool_calls in a single AIMessage,
       ag_ui_langgraph only tracks one ``current_stream.tool_call_id`` at a
       time. Each AIMessageChunk's ``tool_call_chunks[0]`` may belong to a
       *different* index than ``current_stream`` (the LLM has moved on to
       the second/third tool_call), but ARGS events still go out with the
       first tool_call's id. The chat UI ends up appending the second and
       third calls' arg deltas to the first call's segment — visible as
       multiple concatenated JSON objects in one tool's "ARGUMENTS" view.

    This filter:

    * synthesizes TOOL_CALL_START + ARGS + END for any tcid that was *not*
      naturally started by ag_ui_langgraph (the dropped #1 case AND the
      parallel-call siblings beyond the first in #3). Two sources are watched:
      STATE_SNAPSHOT events, and ``on_chat_model_end`` RAW events. The latter
      is required because the ``tools`` node's STATE_SNAPSHOTs only carry the
      freshly-appended ToolMessages — never the parent AIMessage — so the
      snapshot path alone cannot recover parallel siblings,
    * drops misrouted natural ``TOOL_CALL_ARGS`` events whose underlying
      chunk's ``tool_call_chunks[0].index`` doesn't match the chunk index
      recorded when its natural TOOL_CALL_START fired. When case #1 drops
      the index=0 tool's START, the first natural START fires at
      chunk.index>0 and that tool's own args stream at the same index — a
      blanket ``index>0`` drop would discard the only naturally-streamed
      tool's body. Tracking each tcid's natural-start chunk index keeps its
      own deltas while still dropping siblings ag_ui_langgraph misroutes,
    * drops every nested event (``|`` in ns),
    * drops ``REASONING_*`` events whose emitter set ``emit-messages: False``
      (see ``REASONING_EVENT_TYPES``),
    * drops the LATE OnToolEnd re-emitted START/ARGS/END for tool_calls we
      already synthesized (deduping by tool_call_id).
    """

    def __init__(self) -> None:
        self._synthesized: set[str] = set()
        self._natural_started: set[str] = set()
        self._natural_index: dict[str, int] = {}

    async def apply(self, stream: AsyncIterator[BaseEvent]) -> AsyncIterator[BaseEvent]:
        async for event in stream:
            ns = self._checkpoint_ns(event)
            is_nested = "|" in ns

            if is_nested:
                continue

            if event.type in REASONING_EVENT_TYPES and not self._emit_messages(event):
                continue

            if event.type == EventType.STATE_SNAPSHOT:
                # Yield the snapshot first so any state-driven UI updates
                # (e.g. the merge-request pill) commit before the synthesized
                # tool_call segments append to the same turn.
                yield event
                for synth in self._synthesize_unstarted(self._iter_latest_tool_calls(event)):
                    yield synth
                continue

            if event.type == EventType.RAW:
                yield event
                for synth in self._synthesize_unstarted(self._iter_chat_model_end_tool_calls(event)):
                    yield synth
                continue

            if event.type == EventType.TOOL_CALL_ARGS and self._is_misrouted_arg(event):
                continue

            if event.type in (EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END):
                tcid = getattr(event, "tool_call_id", None)
                if isinstance(tcid, str) and tcid in self._synthesized:
                    continue
                if event.type == EventType.TOOL_CALL_START and isinstance(tcid, str):
                    self._natural_started.add(tcid)
                    idx = self._chunk_index(event)
                    if idx is not None:
                        self._natural_index[tcid] = idx

            yield event

    @staticmethod
    def _metadata(event: BaseEvent) -> dict:
        """Return the LangGraph metadata carried by an AGUI event, or ``{}``.

        Upstream attaches the source LangGraph event to ``raw_event`` (RAW events
        carry it on ``event`` instead). ``REASONING_*`` events get neither — they
        only carry metadata because ``RuntimeContextLangGraphAGUIAgent`` stamps a
        minimal ``{"metadata": {...}}`` onto them.
        """
        raw = getattr(event, "raw_event", None)
        if not isinstance(raw, dict):
            raw = getattr(event, "event", None)
        if not isinstance(raw, dict):
            return {}
        md = raw.get("metadata")
        return md if isinstance(md, dict) else {}

    @classmethod
    def _checkpoint_ns(cls, event: BaseEvent) -> str:
        """Extract the LangGraph checkpoint namespace from an AGUI event.

        A ``|`` in the namespace means the event was emitted from a *nested*
        LangGraph execution — i.e. from inside a subagent invoked by the parent's
        ``task`` tool. Top-level events have an empty ns or a single
        ``"<node>:UUID"`` segment (e.g. ``"model:..."``, ``"tools:..."``) with no
        pipe.
        """
        return str(cls._metadata(event).get("langgraph_checkpoint_ns", "") or "")

    @classmethod
    def _emit_messages(cls, event: BaseEvent) -> bool:
        """Whether the emitting runnable opted into streaming its messages.

        Mirrors upstream exactly: default ``True``, then plain truthiness. The
        default also keeps any event whose metadata never reached us — an unstamped
        event would otherwise vanish.
        """
        return bool(cls._metadata(event).get("emit-messages", True))

    def _is_misrouted_arg(self, event: BaseEvent) -> bool:
        """True if a natural TOOL_CALL_ARGS event's underlying chunk belongs to a
        sibling tool_call. ag_ui_langgraph attributes every streamed arg delta
        to the first naturally-started tcid (its ``current_stream``); the
        chunk's ``tool_call_chunks[0].index`` identifies which tool the delta
        actually belongs to. We compare against the index recorded when this
        tcid's natural TOOL_CALL_START fired — the static "index > 0" rule
        only holds when the natural START claimed index=0.
        """
        tcid = getattr(event, "tool_call_id", None)
        if not isinstance(tcid, str):
            return False
        chunk_idx = self._chunk_index(event)
        if chunk_idx is None:
            return False
        natural_idx = self._natural_index.get(tcid)
        if natural_idx is None:
            # No natural START recorded for this tcid yet (e.g. ag_ui_langgraph
            # dropped it via the text→tool_call transition). Fall back to the
            # original heuristic; synthesis at on_chat_model_end will still
            # repair the tcid's args from the AIMessage output.
            return chunk_idx > 0
        return chunk_idx != natural_idx

    @classmethod
    def _chunk_index(cls, event: BaseEvent) -> int | None:
        """Read ``raw_event.data.chunk.tool_call_chunks[0].index`` if present.

        Returns ``None`` for any event whose payload doesn't carry a streaming
        chunk (e.g. STATE_SNAPSHOT, RAW, the OnToolEnd re-emit whose raw_event
        carries ``data.input``/``data.output`` but no ``chunk``).
        """
        raw = getattr(event, "raw_event", None)
        if not isinstance(raw, dict):
            return None
        chunk = (raw.get("data") or {}).get("chunk")
        if chunk is None:
            return None
        tcc = cls._field(chunk, "tool_call_chunks") or []
        if not tcc:
            return None
        idx = cls._field(tcc[0], "index")
        return idx if isinstance(idx, int) else None

    def _synthesize_unstarted(self, tool_calls: Iterable[tuple[str, str, Any]]) -> list[BaseEvent]:
        """Build START + (optional ARGS) + END events for every tcid not yet
        emitted, recording each in ``_synthesized``. Returns a list (not a
        generator) so callers can simply iterate-and-yield without juggling
        ``yield from`` inside an async generator.
        """
        events: list[BaseEvent] = []
        for tcid, name, args in tool_calls:
            if tcid in self._synthesized or tcid in self._natural_started:
                continue
            events.append(ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=tcid, tool_call_name=name))
            if args:
                # ``default=str`` so a Pydantic model / datetime / other
                # non-JSON-native object in args doesn't kill the entire chat
                # stream — better a stringified field than RUN_ERROR.
                delta = args if isinstance(args, str) else json.dumps(args, default=str)
                events.append(ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=tcid, delta=delta))
            events.append(ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tcid))
            self._synthesized.add(tcid)
        return events

    @classmethod
    def _iter_tool_calls_on(cls, message: Any) -> Iterable[tuple[str, str, Any]]:
        """Yield ``(tool_call_id, name, args)`` for every well-formed tool_call
        on a single AIMessage (dict or BaseMessage instance).
        """
        for tc in cls._field(message, "tool_calls") or []:
            tcid = cls._field(tc, "id")
            name = cls._field(tc, "name")
            if isinstance(tcid, str) and isinstance(name, str):
                yield tcid, name, cls._field(tc, "args")

    def _synthesize_unstarted(self, tool_calls: Iterable[tuple[str, str, Any]]) -> list[BaseEvent]:
        """Build START + (optional ARGS) + END events for every tcid not yet
        emitted, recording each in ``_synthesized``. Returns a list (not a
        generator) so callers can simply iterate-and-yield without juggling
        ``yield from`` inside an async generator.
        """
        events: list[BaseEvent] = []
        for tcid, name, args in tool_calls:
            if tcid in self._synthesized or tcid in self._natural_started:
                continue
            events.append(ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=tcid, tool_call_name=name))
            if args:
                # ``default=str`` so a Pydantic model / datetime / other
                # non-JSON-native object in args doesn't kill the entire chat
                # stream — better a stringified field than RUN_ERROR.
                delta = args if isinstance(args, str) else json.dumps(args, default=str)
                events.append(ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=tcid, delta=delta))
            events.append(ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tcid))
            self._synthesized.add(tcid)
        return events

    @classmethod
    def _iter_tool_calls_on(cls, message: Any) -> Iterable[tuple[str, str, Any]]:
        """Yield ``(tool_call_id, name, args)`` for every well-formed tool_call
        on a single AIMessage (dict or BaseMessage instance).
        """
        for tc in cls._field(message, "tool_calls") or []:
            tcid = cls._field(tc, "id")
            name = cls._field(tc, "name")
            if isinstance(tcid, str) and isinstance(name, str):
                yield tcid, name, cls._field(tc, "args")

    @classmethod
    def _iter_latest_tool_calls(cls, event: BaseEvent) -> Iterable[tuple[str, str, Any]]:
        """Yield tool_calls from the snapshot's latest AIMessage. Older AIMessages
        are skipped — their tool_calls are already in ``_synthesized`` /
        ``_natural_started`` from prior snapshots.
        """
        snap = getattr(event, "snapshot", None)
        if not isinstance(snap, dict):
            return
        msgs = snap.get("messages")
        if not isinstance(msgs, list):
            return
        for m in reversed(msgs):
            if cls._msg_role(m) not in ("ai", "assistant"):
                continue
            yield from cls._iter_tool_calls_on(m)
            return

    @classmethod
    def _iter_chat_model_end_tool_calls(cls, event: BaseEvent) -> Iterable[tuple[str, str, Any]]:
        """Yield tool_calls from the AIMessage output of an ``on_chat_model_end``
        RAW event. Empty for any other event shape.
        """
        if event.type != EventType.RAW:
            return
        raw = getattr(event, "event", None)
        if not isinstance(raw, dict) or raw.get("event") != "on_chat_model_end":
            return
        output = (raw.get("data") or {}).get("output")
        if output is not None:
            yield from cls._iter_tool_calls_on(output)

    @staticmethod
    def _field(obj: Any, name: str, default: Any = None) -> Any:
        """Read a field from either a dict or an object with attributes.

        STATE_SNAPSHOT messages and AIMessageChunk fields can carry either
        shape depending on whether the snapshot has been serialized yet —
        the AGUI encoder turns objects into dicts, but this filter sits in
        front of the encoder.
        """
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @classmethod
    def _msg_role(cls, message: Any) -> str:
        return str(cls._field(message, "type", "") or cls._field(message, "role", "") or "").lower()
