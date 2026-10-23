from __future__ import annotations

import logging
from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, Any

from ag_ui.core.events import BaseEvent, EventType, TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent
from copilotkit import LangGraphAGUIAgent

from automation.agent.events import ASSISTANT_MESSAGE_EVENT, parse_assistant_message

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from automation.agent.context import RuntimeCtx

logger = logging.getLogger("daiv.chat")

# GitState fields that survive the ag-ui output-schema filter and reach the
# chat client through STATE_SNAPSHOT events.
STREAMED_STATE_KEYS = ("merge_request", "diff_stats")


class RuntimeContextLangGraphAGUIAgent(LangGraphAGUIAgent):
    """Inject the daiv RuntimeCtx dataclass into upstream's stream kwargs.

    Upstream's ``get_stream_kwargs`` only accepts dict-shaped contexts, but our graph
    declares ``context_schema=RuntimeCtx`` and expects the frozen dataclass itself.

    With ``graph_input``, the graph runs on that input rather than the one upstream derives from
    ``RunAgentInput.messages``, which forks the thread when a message id is already checkpointed.
    """

    def __init__(self, *, runtime_context: RuntimeCtx, graph_input: dict[str, Any] | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._runtime_context = runtime_context
        self._graph_input = graph_input

    def get_stream_kwargs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        stream_kwargs = super().get_stream_kwargs(*args, **kwargs)
        # Hard-assign (not setdefault): newer LangGraph makes upstream synthesize
        # ``context={"thread_id": ...}`` from ``config['configurable']``, and LangGraph coerces a
        # dict context via ``context_schema(**context)`` -> ``RuntimeCtx(thread_id=...)`` -> TypeError.
        # Passing the dataclass instance skips coercion; ``thread_id`` still reaches the checkpointer
        # through ``config['configurable']``.
        stream_kwargs["context"] = self._runtime_context
        if self._graph_input is not None:
            stream_kwargs["input"] = self._graph_input
        return stream_kwargs

    async def _handle_single_event(self, event: Any, state: dict[str, Any]) -> AsyncGenerator[Any]:
        """Stamp the source LangGraph event's provenance onto any AGUI event upstream
        emits without a ``raw_event``.

        ``ag_ui_langgraph`` builds every event type with ``raw_event=event`` *except*
        the ``REASONING_*`` family (``handle_reasoning_event`` and the redacted-thinking
        ``ReasoningEncryptedValueEvent``), which it emits bare. ``SubagentEventFilter``
        reads provenance exclusively from ``raw_event.metadata.langgraph_checkpoint_ns``,
        so untagged reasoning reads back as top-level: a subagent's *thinking* bleeds
        into the parent turn even though its text and tool calls are correctly dropped.

        Stamping here rather than inferring the namespace in the filter is deliberate,
        but *not* because the filter lacks the information: upstream yields a RAW event
        carrying the same metadata immediately before dispatching each LangGraph event
        (``agent.py`` ``_handle_stream_events``), so a "namespace of the last event that
        had one" tracker would work today. It would just be betting on undocumented
        yield ordering and on that RAW emission staying unconditional. Carrying
        provenance *on* the event removes the bet.

        Only the two keys the filter consumes are copied — stamping the whole metadata
        dict would serialize ~550 bytes of LangGraph internals onto every reasoning
        delta, and a thinking stream emits one event per delta.

        Known limit: upstream tracks reasoning state per *run*, not per namespace
        (``active_run["reasoning_process"]``), so the closing REASONING_END /
        REASONING_ENCRYPTED_VALUE frames are stamped with whatever chunk is in flight
        when they fire. A subagent whose reasoning is still open when its model stream
        ends can therefore emit a stray unmatched END under the parent's namespace —
        a dangling frame, never readable content.
        """
        metadata = event.get("metadata") if isinstance(event, dict) else None
        if not isinstance(metadata, dict):
            metadata = {}
        provenance = {k: metadata[k] for k in ("langgraph_checkpoint_ns", "emit-messages") if k in metadata}

        async for agui_event in super()._handle_single_event(event, state):
            # The isinstance check is always true at runtime — every member of
            # upstream's ``ProcessedEvents`` union subclasses ``BaseEvent``. Keep it
            # anyway: ``copilotkit.LangGraphAGUIAgent`` *declares* this method as
            # yielding ``str``, so it is the only form that narrows the loop variable
            # for ``ty``. Dropping it reintroduces "unresolved attribute raw_event
            # on type str"; a bare ``agui_event: BaseEvent`` declaration conflicts
            # with the inherited signature instead.
            if provenance and isinstance(agui_event, BaseEvent) and agui_event.raw_event is None:
                agui_event.raw_event = {"metadata": provenance}
            yield agui_event

        for text_event in self._assistant_message_frames(event, provenance):
            yield text_event

    @staticmethod
    def _assistant_message_frames(event: Any, provenance: dict) -> list[BaseEvent]:
        """Translate DAIV's ``ASSISTANT_MESSAGE_EVENT`` into the text frames the chat renders.

        See ``automation.agent.utils.streamed_assistant_message`` for why a message the agent
        produced without a model call never streams on its own.

        This belongs on the event hook rather than in ``SubagentEventFilter``: the frames carry the
        source event's ``langgraph_checkpoint_ns``, so the filter already drops them when the
        message came from inside a subagent. Consolidating the two would lose that for free.

        ``ag_ui_langgraph`` ships an equivalent handler for its own ``manually_emit_message``, and
        reusing it would need no import — only that wire name. It is forked anyway, for two reasons
        that are easy to miss: upstream hard-subscripts ``event["data"]["message_id"]``, so a single
        malformed payload raises mid-stream and kills the run, and adopting the name would make an
        undocumented upstream enum load-bearing, where a rename would silently stop these messages
        rendering — the exact failure this whole change exists to fix. Copilotkit's
        ``copilotkit_``-prefixed variant is separately unusable: its handler builds these three
        frames and then discards them, dropping what ``_dispatch_event`` returns instead of
        yielding it.
        """
        if not isinstance(event, dict) or event.get("event") != "on_custom_event":
            return []
        if event.get("name") != ASSISTANT_MESSAGE_EVENT:
            return []
        if (parsed := parse_assistant_message(event.get("data"))) is None:
            logger.warning("chat: malformed %s payload; dropping frames", ASSISTANT_MESSAGE_EVENT)
            return []
        # Only the keys the filter reads: stamping the whole event would ship the LangGraph
        # internals *and* a second copy of the message body on each of the three frames.
        raw_event = {"metadata": provenance}
        return [
            TextMessageStartEvent(
                type=EventType.TEXT_MESSAGE_START, role="assistant", message_id=parsed.message_id, raw_event=raw_event
            ),
            TextMessageContentEvent(
                type=EventType.TEXT_MESSAGE_CONTENT,
                message_id=parsed.message_id,
                delta=parsed.content,
                raw_event=raw_event,
            ),
            TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=parsed.message_id, raw_event=raw_event),
        ]

    def get_schema_keys(self, config: Any) -> dict[str, list[str]]:
        # Upstream calls ``graph.config_schema().schema()`` which recurses into
        # ``context_schema=RuntimeCtx``. RuntimeCtx holds a ``git.Repo`` field that
        # pydantic cannot turn into JSON schema, so the call raises
        # PydanticInvalidForJsonSchema. Derive context keys from the dataclass directly.
        ctx_schema = getattr(self.graph, "context_schema", None)
        if not is_dataclass(ctx_schema):
            logger.warning(
                "chat: context_schema %r is not a dataclass; STATE_SNAPSHOT context keys will be empty", ctx_schema
            )
            context_keys: list[str] = []
        else:
            context_keys = [f.name for f in fields(ctx_schema)]
        constant = list(self.constant_schema_keys)
        return {"input": constant, "output": [*constant, *STREAMED_STATE_KEYS], "config": [], "context": context_keys}
