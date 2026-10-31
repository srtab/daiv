"""The agent → chat signalling channel: events the agent emits for the transport to translate.

Each event's wire shape is defined here alone, so the two ends cannot drift: every consumer
degrades a payload it cannot read to "no frames" (or "no update") rather than raising, so a key
renamed on one side alone fails silently — empty turns for the assistant message, a frozen meter
for context usage. The consumer half lives with each event: Python (``sessions.live.agui``,
parsing through this module) for the assistant message, JS (``chat-stream.js``, whose node test
drives the handler with payloads built here) for the context meter and the publish result, which
is also kept per run by :func:`record_publish_result`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NamedTuple

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from uuid import UUID

    from automation.agent.publishers import PublishOutcome
    from automation.agent.usage_tracking import ResolvedWindow
    from codebase.base import MergeRequest, MergeRequestDiffStats

# Custom LangGraph event carrying an assistant message the agent produced *without* a model call
# (a slash command reply, a loop-breaker stop). Ignored by the webhook and MCP transports.
ASSISTANT_MESSAGE_EVENT = "daiv_assistant_message"


class AssistantMessage(NamedTuple):
    """The payload of an :data:`ASSISTANT_MESSAGE_EVENT`, parsed."""

    message_id: str
    content: str


def assistant_message_payload(message_id: str, content: str) -> dict[str, str]:
    """Build the wire payload for :data:`ASSISTANT_MESSAGE_EVENT`."""
    return {"message_id": message_id, "message": content}


def parse_assistant_message(data: Any) -> AssistantMessage | None:
    """Read an :data:`ASSISTANT_MESSAGE_EVENT` payload, or ``None`` if it is not well-formed.

    Anything malformed is a bug on the producing side, never user input, so the caller drops the
    event instead of emitting frames a client could not close.
    """
    if not isinstance(data, dict):
        return None
    message_id, content = data.get("message_id"), data.get("message")
    if not isinstance(message_id, str) or not isinstance(content, str):
        return None
    return AssistantMessage(message_id=message_id, content=content)


# Custom LangGraph event carrying the last main-model call's usage, for the chat's context
# meter. Ignored by the webhook and MCP transports, like ASSISTANT_MESSAGE_EVENT.
CONTEXT_USAGE_EVENT = "daiv_context_usage"


_SERVICE_TIERS = ("priority", "flex")


def input_detail_tokens(usage_metadata: Mapping[str, Any], key: str) -> int:
    """``input_token_details[key]``, plus the ``priority_``/``flex_`` variants langchain-openai writes instead
    when the response reports that ``service_tier`` (OpenRouter does for its "fast" endpoints)."""
    details = usage_metadata.get("input_token_details") or {}
    return sum(details.get(name) or 0 for name in (key, *(f"{tier}_{key}" for tier in _SERVICE_TIERS)))


def context_usage_payload(*, model: str, usage: Mapping[str, Any], window: ResolvedWindow | None) -> dict[str, Any]:
    """Build the wire payload for :data:`CONTEXT_USAGE_EVENT` from a message's ``usage_metadata``.

    Takes the raw mapping so the token-key knowledge — including where providers bury the
    cache-read count — lives here once, shared by the live middleware and the hydration seed.

    Deliberately no parse half: this payload has no Python consumer — ``ag_ui_langgraph``
    forwards it to the browser as an untranslated ``CustomEvent``, so the other half of the
    contract is JavaScript (``chat-stream.js``), whose node test drives the handler with a
    payload built here rather than a hand-written fixture.
    """
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    return {
        # Diagnostic only — the browser normalizes it away. Kept as the one observable that the
        # streamed-duplication repair ran before the window was resolved from this same name.
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": input_detail_tokens(usage, "cache_read"),
        "used_tokens": input_tokens + output_tokens,
        "window_tokens": window.tokens if window else None,
    }


PUBLISH_RESULT_EVENT = "daiv_publish_result"


def publish_result_payload(
    *,
    status: str,
    merge_request: MergeRequest | None = None,
    diff_stats: MergeRequestDiffStats | None = None,
    error_kind: str | None = None,
) -> dict[str, Any]:
    """Build the wire payload for :data:`PUBLISH_RESULT_EVENT`; values that do not apply are ``None``."""
    return {
        "status": status,
        "merge_request_iid": merge_request.merge_request_id if merge_request else None,
        "merge_request_web_url": merge_request.web_url if merge_request else None,
        "files_changed": diff_stats.files_changed if diff_stats else None,
        "lines_added": diff_stats.lines_added if diff_stats else None,
        "lines_removed": diff_stats.lines_removed if diff_stats else None,
        "error_kind": error_kind,
    }


def published_outcome_payload(outcome: PublishOutcome) -> dict[str, Any]:
    """The :func:`publish_result_payload` of a publish that pushed ``outcome``."""
    if outcome.merge_request is None:
        return publish_result_payload(status="pushed", diff_stats=outcome.diff_stats, error_kind="mr_pending")
    return publish_result_payload(
        status="opened" if outcome.created_merge_request else "pushed",
        merge_request=outcome.merge_request,
        diff_stats=outcome.diff_stats,
    )


class PublishResultRecorder(BaseCallbackHandler):
    """Keeps the last :data:`PUBLISH_RESULT_EVENT` payload dispatched while :func:`record_publish_result` is active."""

    run_inline = True
    # Custom events only: an unhandled chat-model start makes langchain stringify the whole prompt per call.
    ignore_llm = ignore_chat_model = ignore_chain = ignore_agent = ignore_retriever = ignore_retry = True

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    def on_custom_event(
        self,
        name: str,
        data: Any,
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if name == PUBLISH_RESULT_EVENT and isinstance(data, dict):
            self.payload = data


# Once, at import: a hook registered per block would pile up in langchain's module-level hook list.
_publish_result_recorder: ContextVar[PublishResultRecorder | None] = ContextVar(
    "daiv_publish_result_recorder", default=None
)
register_configure_hook(_publish_result_recorder, inheritable=True)


@contextmanager
def record_publish_result() -> Generator[PublishResultRecorder]:
    """Record the publish results dispatched by every runnable invoked inside the block, so a caller reads what this
    run published rather than a checkpoint that can still hold the previous run's."""
    recorder = PublishResultRecorder()
    token = _publish_result_recorder.set(recorder)
    try:
        yield recorder
    finally:
        _publish_result_recorder.reset(token)
