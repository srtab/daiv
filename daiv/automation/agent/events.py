"""The agent → chat signalling channel: events the agent emits for the transport to translate.

Each event's wire shape is defined here alone, so the two ends cannot drift: every consumer
degrades a payload it cannot read to "no frames" (or "no update") rather than raising, so a key
renamed on one side alone fails silently — empty turns for the assistant message, a frozen meter
for context usage. The consumer half lives with each event: Python (``chat.api.streaming``,
parsing through this module) for the assistant message, JS (``chat-stream.js``, whose node test
drives the handler with payloads built here) for the context meter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping

    from automation.agent.usage_tracking import ResolvedWindow

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
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": (usage.get("input_token_details") or {}).get("cache_read", 0),
        "used_tokens": input_tokens + output_tokens,
        "window_tokens": window.tokens if window else None,
    }
