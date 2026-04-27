import json
from typing import TYPE_CHECKING, Any

from ag_ui.core.events import EventType, ToolCallArgsEvent, ToolCallEndEvent, ToolCallStartEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from ag_ui.core.events import BaseEvent


class SubagentEventFilter:
    """Reorder/suppress AGUI events so subagent frames don't leak into the parent turn.

    Two upstream behaviors collide on ``task``-tool turns:

    1. ag_ui_langgraph drops the parent's ``task`` TOOL_CALL_START on the
       text→tool_call transition chunk (the chunk that ends the parent's text
       stream also carries the new tool_call name, but the handler returns
       after emitting TEXT_MESSAGE_END). Subsequent chunks only have args, so
       OnChatModelStream never reaches ``is_tool_call_start_event``. The
       ``task`` TOOL_CALL_START finally arrives from the OnToolEnd re-emit —
       *after* the subagent has already streamed text/tool calls to the
       client.

    2. With ``stream_subgraphs=True``, every chunk emitted from inside
       ``subagent.ainvoke()`` flows through the parent's stream with a
       nested ``langgraph_checkpoint_ns`` (``"tools:UUID|model:UUID"``).
       Without (1)'s TOOL_CALL_START there is no ``task`` segment to
       suppress them against.

    This filter:

    * captures ``task`` tool_call ids from top-level STATE_SNAPSHOT events,
    * synthesizes TOOL_CALL_START + ARGS + END for each on the first nested
      event so the chat creates the segment *before* the subagent runs,
    * drops every nested event (``|`` in ns),
    * drops the LATE OnToolEnd re-emitted START/ARGS/END for tool_calls we
      already synthesized (deduping by tool_call_id).

    The parent's TOOL_CALL_RESULT for the task tool still flows through
    untouched — it's a top-level event with the same ``tool_call_id``, so the
    chat UI flips the synthesized segment to ``done`` exactly like a normal
    tool call.
    """

    # Tool name used by deepagents' SubAgentMiddleware to invoke a subagent.
    TASK_TOOL_NAME = "task"

    def __init__(self) -> None:
        # Two-state lifecycle: a tool_call_id starts in ``_pending`` (synthesize
        # on next nested event), then moves to ``_emitted`` (drop the late
        # re-emit). Membership in either is enough to dedup a STATE_SNAPSHOT
        # rebroadcast; ``_emitted`` alone gates the late TOOL_CALL_*
        # re-emit drop.
        self._pending: dict[str, tuple[str, Any]] = {}
        self._emitted: set[str] = set()

    async def apply(self, stream: AsyncIterator[BaseEvent]) -> AsyncIterator[BaseEvent]:
        async for event in stream:
            ns = self._checkpoint_ns(event)
            is_nested = "|" in ns

            if not is_nested and event.type == EventType.STATE_SNAPSHOT:
                for tcid, name, args in self._iter_latest_task_calls(event):
                    if tcid not in self._pending and tcid not in self._emitted:
                        self._pending[tcid] = (name, args)

            if is_nested:
                for tcid, (name, args) in self._pending.items():
                    yield ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=tcid, tool_call_name=name)
                    if args:
                        # ``default=str`` so a Pydantic model / datetime / other
                        # non-JSON-native object in args doesn't kill the entire
                        # chat stream — better a stringified field than RUN_ERROR.
                        delta = args if isinstance(args, str) else json.dumps(args, default=str)
                        yield ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=tcid, delta=delta)
                    yield ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tcid)
                    self._emitted.add(tcid)
                self._pending.clear()
                continue

            if event.type in (EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END):
                tcid = getattr(event, "tool_call_id", None)
                if isinstance(tcid, str) and tcid in self._emitted:
                    continue

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
    def _iter_latest_task_calls(cls, event: BaseEvent) -> Iterable[tuple[str, str, Any]]:
        """Yield ``(tool_call_id, name, args)`` for every ``task`` tool_call on the
        snapshot's latest AIMessage. Caller is responsible for dedup against
        already-emitted ids — this is just the per-snapshot scan.
        """
        snap = getattr(event, "snapshot", None)
        if not isinstance(snap, dict):
            return
        msgs = snap.get("messages")
        if not isinstance(msgs, list):
            return
        # Only the latest AIMessage matters — older AIMessages were emitted on
        # earlier snapshots and their task ids are already in ``task_calls``.
        # Walking past the latest is just wasted work; the dedup map at the call
        # site is what guarantees no double-synthesis.
        for m in reversed(msgs):
            if cls._msg_role(m) not in ("ai", "assistant"):
                continue
            for tc in cls._msg_field(m, "tool_calls") or []:
                tcid = cls._msg_field(tc, "id")
                name = cls._msg_field(tc, "name")
                if name == cls.TASK_TOOL_NAME and isinstance(tcid, str):
                    yield tcid, name, cls._msg_field(tc, "args")
            return

    @staticmethod
    def _msg_field(message: Any, name: str, default: Any = None) -> Any:
        """Read a field from a LangChain message or its dict-encoded form.

        STATE_SNAPSHOT can carry either shape depending on whether the snapshot
        has been serialized yet — running through the AGUI encoder turns objects
        into dicts, but the filter here sits *before* the encoder.
        """
        if isinstance(message, dict):
            return message.get(name, default)
        return getattr(message, name, default)

    @classmethod
    def _msg_role(cls, message: Any) -> str:
        return str(cls._msg_field(message, "type", "") or cls._msg_field(message, "role", "") or "").lower()
