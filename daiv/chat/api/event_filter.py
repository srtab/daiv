from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ag_ui.core.events import EventType, ToolCallArgsEvent, ToolCallEndEvent, ToolCallStartEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from ag_ui.core.events import BaseEvent


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

    3. When the LLM emits parallel tool_calls in a single AIMessage,
       ag_ui_langgraph only tracks one ``current_stream.tool_call_id`` at a
       time. Each AIMessageChunk's ``tool_call_chunks[0]`` may belong to a
       *different* index than ``current_stream`` (the LLM has moved on to
       the second/third tool_call), but ARGS events still go out with the
       first tool_call's id. The chat UI ends up appending the second and
       third calls' arg deltas to the first call's segment — visible as
       multiple concatenated JSON objects in one tool's "ARGUMENTS" view.

    This filter:

    * captures every top-level tool_call from STATE_SNAPSHOT events and
      synthesizes TOOL_CALL_START + ARGS + END for any tcid that was *not*
      naturally started by ag_ui_langgraph (the dropped #1 case AND the
      parallel-call siblings beyond the first in #3),
    * drops misrouted natural ``TOOL_CALL_ARGS`` events whose underlying
      chunk's ``tool_call_chunks[0].index > 0`` — the delta belongs to a
      sibling, not to the tcid in the event,
    * drops every nested event (``|`` in ns),
    * drops the LATE OnToolEnd re-emitted START/ARGS/END for tool_calls we
      already synthesized (deduping by tool_call_id).
    """

    def __init__(self) -> None:
        self._synthesized: set[str] = set()
        self._natural_started: set[str] = set()
        self._natural_ended: set[str] = set()

    async def apply(self, stream: AsyncIterator[BaseEvent]) -> AsyncIterator[BaseEvent]:
        async for event in stream:
            ns = self._checkpoint_ns(event)
            is_nested = "|" in ns

            if is_nested:
                continue

            if event.type == EventType.STATE_SNAPSHOT:
                # Yield the snapshot first so any state-driven UI updates
                # (e.g. the merge-request pill) commit before the synthesized
                # tool_call segments append to the same turn.
                yield event

                pending: dict[str, tuple[str, Any]] = {}
                for tcid, name, args in self._iter_latest_tool_calls(event):
                    if tcid in self._synthesized or tcid in self._natural_started:
                        continue
                    pending[tcid] = (name, args)

                for tcid, (name, args) in pending.items():
                    yield ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=tcid, tool_call_name=name)
                    if args:
                        # ``default=str`` so a Pydantic model / datetime / other
                        # non-JSON-native object in args doesn't kill the entire
                        # chat stream — better a stringified field than RUN_ERROR.
                        delta = args if isinstance(args, str) else json.dumps(args, default=str)
                        yield ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=tcid, delta=delta)
                    yield ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tcid)
                    self._synthesized.add(tcid)
                continue

            if event.type == EventType.TOOL_CALL_ARGS and self._is_misrouted_arg(event):
                continue

            if event.type in (EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END):
                tcid = getattr(event, "tool_call_id", None)
                if isinstance(tcid, str) and tcid in self._synthesized:
                    continue
                if isinstance(tcid, str) and tcid in self._natural_ended:
                    # OnToolEnd re-emits the full Start/Args/End triple for tool_calls
                    # that already streamed their natural lifecycle. The first pass is
                    # authoritative — CopilotKit's tool-call state machine has already
                    # rendered ``complete``; re-emitting would either double-render or
                    # flip the segment back to ``running``. Mirrors the synthesized
                    # branch above (synthesized START suppresses the natural one;
                    # natural END suppresses the re-emitted one).
                    continue
                if event.type == EventType.TOOL_CALL_START and isinstance(tcid, str):
                    self._natural_started.add(tcid)
                if event.type == EventType.TOOL_CALL_END and isinstance(tcid, str) and tcid in self._natural_started:
                    self._natural_ended.add(tcid)

            yield event

    @staticmethod
    def _checkpoint_ns(event: BaseEvent) -> str:
        """Extract the LangGraph checkpoint namespace from an AGUI event's raw_event.

        A ``|`` in the namespace means the event was emitted from a *nested*
        LangGraph execution — i.e. from inside a subagent invoked by the parent's
        ``task`` tool. Top-level events have an empty ns or a single
        ``"<node>:UUID"`` segment (e.g. ``"model:..."``, ``"tools:..."``) with no
        pipe.
        """
        raw = getattr(event, "raw_event", None)
        if not isinstance(raw, dict):
            return ""
        md = raw.get("metadata") or {}
        return str(md.get("langgraph_checkpoint_ns", "") or "")

    @classmethod
    def _is_misrouted_arg(cls, event: BaseEvent) -> bool:
        """True if a natural TOOL_CALL_ARGS event's underlying chunk belongs to a
        sibling tool_call (chunk index > 0) but ag_ui_langgraph attributed it to
        the first call's tool_call_id.
        """
        raw = getattr(event, "raw_event", None)
        if not isinstance(raw, dict):
            return False
        chunk = (raw.get("data") or {}).get("chunk")
        if chunk is None:
            return False
        tcc = cls._field(chunk, "tool_call_chunks") or []
        if not tcc:
            return False
        idx = cls._field(tcc[0], "index")
        return isinstance(idx, int) and idx > 0

    @classmethod
    def _iter_latest_tool_calls(cls, event: BaseEvent) -> Iterable[tuple[str, str, Any]]:
        """Yield ``(tool_call_id, name, args)`` for every tool_call on the
        snapshot's latest AIMessage. Caller is responsible for dedup against
        already-emitted ids — this is just the per-snapshot scan.
        """
        snap = getattr(event, "snapshot", None)
        if not isinstance(snap, dict):
            return
        msgs = snap.get("messages")
        if not isinstance(msgs, list):
            return
        # Only the latest AIMessage matters — older AIMessages' tool_calls are
        # already in ``_synthesized`` / ``_natural_started`` from prior snapshots.
        for m in reversed(msgs):
            if cls._msg_role(m) not in ("ai", "assistant"):
                continue
            for tc in cls._field(m, "tool_calls") or []:
                tcid = cls._field(tc, "id")
                name = cls._field(tc, "name")
                if isinstance(tcid, str) and isinstance(name, str):
                    yield tcid, name, cls._field(tc, "args")
            return

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
