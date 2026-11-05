from __future__ import annotations

import logging
import re
from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, Any

from ag_ui.core.events import (
    BaseEvent,
    EventType,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallResultEvent,
)
from ag_ui_langgraph.utils import normalize_tool_content
from copilotkit import LangGraphAGUIAgent
from langchain_core.messages import ToolMessage

from automation.agent.events import ASSISTANT_MESSAGE_EVENT, parse_assistant_message

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from ag_ui.core import RunAgentInput
    from ag_ui_langgraph.agent import PreparedStream
    from langchain_core.runnables import RunnableConfig

    from automation.agent.context import RuntimeCtx

logger = logging.getLogger("daiv.sessions")

# GitState fields that survive the ag-ui output-schema filter and reach the
# chat client through STATE_SNAPSHOT events.
STREAMED_STATE_KEYS = ("merge_request", "diff_stats")

_FILTERED_DATA = {"on_chat_model_stream": "chunk", "on_chat_model_end": "output"}

_ERROR_PREFIX = re.compile(r"\s*error\b", re.IGNORECASE)


def _filter_view(event: Any) -> Any:
    """What ``SubagentEventFilter`` and copilotkit read of a LangGraph event: upstream would deep-convert the rest
    (the run's state, the model's input) on every event, only for the relay to drop it."""
    if not isinstance(event, dict):
        return event
    view = {key: event[key] for key in ("event", "name", "metadata") if key in event}
    data, kept = event.get("data"), _FILTERED_DATA.get(event.get("event"))
    if kept and isinstance(data, dict) and kept in data:
        view["data"] = {kept: data[kept]}
    return view


class RuntimeContextLangGraphAGUIAgent(LangGraphAGUIAgent):
    """Inject the daiv RuntimeCtx dataclass into upstream's stream kwargs.

    Upstream's ``get_stream_kwargs`` only accepts dict-shaped contexts, but our graph
    declares ``context_schema=RuntimeCtx`` and expects the frozen dataclass itself.

    With ``graph_input``, the graph runs on that input rather than the one upstream derives from
    ``RunAgentInput.messages``, which forks the thread when a message id is already checkpointed.

    ``graph_stream`` is the graph's own event stream once upstream has prepared it. Upstream abandons it unclosed when
    translating an event raises, so a caller can finish or close the graph run whatever upstream did.
    """

    def __init__(self, *, runtime_context: RuntimeCtx, graph_input: dict[str, Any] | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._runtime_context = runtime_context
        self._graph_input = graph_input
        self.graph_stream: AsyncGenerator[Any] | None = None
        self._answered_tool_calls: set[str] = set()

    async def prepare_stream(self, input: RunAgentInput, agent_state: Any, config: RunnableConfig) -> PreparedStream:  # noqa: A002
        if self._graph_input is not None:
            # Upstream replays an open interrupt instead of running the graph; ``ainvoke`` runs the new input.
            agent_state = agent_state._replace(tasks=())
        prepared = await super().prepare_stream(input=input, agent_state=agent_state, config=config)
        self.graph_stream = prepared["stream"]
        return prepared

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

    def _dispatch_event(self, event: Any) -> Any:
        if event.type == EventType.RAW:
            event.event = _filter_view(event.event)
        elif event.raw_event:
            event.raw_event = _filter_view(event.raw_event)
        return super()._dispatch_event(event)

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
            if isinstance(agui_event, ToolCallResultEvent):
                if agui_event.tool_call_id in self._answered_tool_calls:
                    continue
                self._answered_tool_calls.add(agui_event.tool_call_id)
            yield agui_event

        for text_event in self._assistant_message_frames(event, provenance):
            yield text_event
        for result_event in self._unanswered_tool_results(event, provenance):
            yield result_event

    def _unanswered_tool_results(self, event: Any, provenance: dict) -> list[BaseEvent]:
        """TOOL_CALL_RESULT for each ToolMessage the tools node returned that upstream never sent one for.

        Upstream emits RESULT only from ``on_tool_end``, which never fires for a call a middleware answers without
        running the tool (deepagents' parallel same-file edit guard), so the chat row would spin until the run ends.
        ``on_tool_end`` always precedes the node's ``on_chain_end``, whose output holds only that step's messages.
        Stamped like ``_assistant_message_frames``, so the filter drops a subagent's.
        """
        if not isinstance(event, dict) or event.get("event") != "on_chain_end" or event.get("name") != "tools":
            return []
        output = (event.get("data") or {}).get("output")
        messages = output.get("messages") if isinstance(output, dict) else None
        results: list[BaseEvent] = []
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, ToolMessage) or message.tool_call_id in self._answered_tool_calls:
                continue
            self._answered_tool_calls.add(message.tool_call_id)
            content = normalize_tool_content(message.content)
            # The event has no status: the chat UI marks a row failed only by this prefix (``ERROR_PREFIX_RE``).
            if message.status == "error" and not _ERROR_PREFIX.match(content):
                content = f"Error: {content}"
            results.append(
                ToolCallResultEvent(
                    type=EventType.TOOL_CALL_RESULT,
                    tool_call_id=message.tool_call_id,
                    message_id=str(message.id or message.tool_call_id),
                    content=content,
                    role="tool",
                    raw_event={"metadata": provenance},
                )
            )
        return results

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
            logger.warning("live: malformed %s payload; dropping frames", ASSISTANT_MESSAGE_EVENT)
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
                "live: context_schema %r is not a dataclass; STATE_SNAPSHOT context keys will be empty", ctx_schema
            )
            context_keys: list[str] = []
        else:
            context_keys = [f.name for f in fields(ctx_schema)]
        constant = list(self.constant_schema_keys)
        return {"input": constant, "output": [*constant, *STREAMED_STATE_KEYS], "config": [], "context": context_keys}
