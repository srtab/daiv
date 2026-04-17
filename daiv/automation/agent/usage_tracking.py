from __future__ import annotations

import dataclasses
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from genai_prices import Usage, calc_price
from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
from langchain_core.tracers.context import register_configure_hook

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger("daiv.usage")

# Registered once at import time. Upstream ``get_usage_metadata_callback`` creates a new
# ContextVar and hook registration on every call, leaking into the module-level hook list.
_usage_metadata_var: ContextVar[UsageMetadataCallbackHandler | None] = ContextVar(
    "daiv_usage_metadata_callback", default=None
)
register_configure_hook(_usage_metadata_var, inheritable=True)


@contextmanager
def track_usage_metadata() -> Iterator[UsageMetadataCallbackHandler]:
    """Activate a ``UsageMetadataCallbackHandler`` for the enclosed block.

    The handler is auto-propagated to every nested ``Runnable`` invocation (including
    subagents) via the registered ``ContextVar`` hook, so callers don't need to thread
    callbacks through ``RunnableConfig``.
    """
    handler = UsageMetadataCallbackHandler()
    token = _usage_metadata_var.set(handler)
    try:
        yield handler
    finally:
        _usage_metadata_var.reset(token)


@dataclass
class UsageSummary:
    """Aggregated token usage and cost across an entire agent run."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: str | None = None
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _calc_model_cost(model_name: str, usage_metadata: dict[str, Any]) -> Decimal | None:
    """Return None if the model is not in the pricing database."""
    input_details = usage_metadata.get("input_token_details") or {}
    genai_usage = Usage(
        input_tokens=usage_metadata.get("input_tokens", 0),
        output_tokens=usage_metadata.get("output_tokens", 0),
        cache_write_tokens=input_details.get("cache_creation") or 0,
        cache_read_tokens=input_details.get("cache_read") or 0,
    )
    # OpenRouter model names contain a slash (e.g. "anthropic/claude-sonnet-4.6")
    # and need an explicit provider_id to resolve correctly.
    provider_id = "openrouter" if "/" in model_name else None
    try:
        result = calc_price(genai_usage, model_ref=model_name, provider_id=provider_id)
    except LookupError:
        logger.warning("No pricing found for model %r", model_name)
        return None
    except Exception:
        logger.warning("Cost calculation failed for model %r", model_name, exc_info=True)
        return None
    return result.total_price


def build_usage_summary(handler_data: dict[str, dict[str, Any]]) -> UsageSummary:
    """Build a UsageSummary from ``UsageMetadataCallbackHandler.usage_metadata``."""
    if not handler_data:
        logger.warning("Usage metadata is empty; callback hook may not have fired on any LLM call")
        return UsageSummary()

    total_input = 0
    total_output = 0
    total_total = 0
    total_cost = Decimal("0")
    all_priced = True
    by_model: dict[str, dict[str, Any]] = {}

    for model_name, usage in handler_data.items():
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        total_input += input_tokens
        total_output += output_tokens
        total_total += total_tokens

        model_entry: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

        if input_details := usage.get("input_token_details"):
            model_entry["input_token_details"] = dict(input_details)
        if output_details := usage.get("output_token_details"):
            model_entry["output_token_details"] = dict(output_details)

        model_cost = _calc_model_cost(model_name, usage)
        if model_cost is not None:
            model_entry["cost_usd"] = str(model_cost)
            total_cost += model_cost
        else:
            all_priced = False

        by_model[model_name] = model_entry

    cost_usd = str(total_cost) if all_priced else None

    logger.info("Usage summary: input=%d output=%d total=%d cost=%s", total_input, total_output, total_total, cost_usd)

    return UsageSummary(
        input_tokens=total_input,
        output_tokens=total_output,
        total_tokens=total_total,
        cost_usd=cost_usd,
        by_model=by_model,
    )
