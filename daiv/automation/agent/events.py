"""The agent → chat signalling channel: events the agent emits for the transport to translate.

Both halves of each event live here. The producer (``automation.agent.utils``) and the consumer
(``chat.api.streaming``) sit in packages with no shared schema, and the consumer degrades a payload
it cannot read to "no frames" rather than raising — so a key renamed on one side alone would go back
to painting empty turns, silently, which is the failure the assistant-message event exists to fix.
Build and parse through these helpers so the two sides cannot drift apart.
"""

from __future__ import annotations

from typing import Any, NamedTuple

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


def context_usage_payload(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    window_tokens: int | None,
    window_source: str | None,
) -> dict[str, Any]:
    """Build the wire payload for :data:`CONTEXT_USAGE_EVENT`.

    Deliberately no parse half: this payload has no Python consumer — ``ag_ui_langgraph``
    forwards it to the browser as an untranslated ``CustomEvent``, so the other half of the
    contract is JavaScript (``chat-stream.js``), whose node test drives the handler with a
    payload built here rather than a hand-written fixture.
    """
    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "used_tokens": input_tokens + output_tokens,
        "window_tokens": window_tokens,
        "window_source": window_source if window_tokens else None,
    }
