from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage

from automation.agent.usage_tracking import UsageSummary, build_usage_summary

if TYPE_CHECKING:
    from collections.abc import Iterable


def aggregate_messages_usage(messages: Iterable[BaseMessage]) -> UsageSummary:
    """Build a UsageSummary from the AIMessages in a thread.

    Reuses ``build_usage_summary`` for both arithmetic and pricing — this function
    only reshapes message-level ``usage_metadata`` into the
    ``{model_name: usage_dict}`` layout that helper expects.
    """
    handler_data: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        usage = getattr(msg, "usage_metadata", None) or {}
        if not usage:
            continue
        model_name = (getattr(msg, "response_metadata", None) or {}).get("model_name") or "unknown"
        bucket = handler_data.setdefault(model_name, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        bucket["input_tokens"] += int(usage.get("input_tokens") or 0)
        bucket["output_tokens"] += int(usage.get("output_tokens") or 0)
        bucket["total_tokens"] += int(usage.get("total_tokens") or 0)
        for key in ("input_token_details", "output_token_details"):
            details = usage.get(key)
            if details:
                bucket.setdefault(key, dict(details))
    return build_usage_summary(handler_data)
