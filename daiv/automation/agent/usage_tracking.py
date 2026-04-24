from __future__ import annotations

import dataclasses
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, override

from genai_prices import Usage, calc_price
from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.tracers.context import register_configure_hook

if TYPE_CHECKING:
    from collections.abc import Iterator

    from langchain_core.outputs import LLMResult

logger = logging.getLogger("daiv.usage")


class DaivUsageCallbackHandler(UsageMetadataCallbackHandler):
    """``UsageMetadataCallbackHandler`` extension that also captures provider-reported cost.

    For OpenRouter responses, ``ChatOpenRouter`` stashes the billed ``cost`` (USD) on
    each message's ``response_metadata`` (under ``openrouter_cost_usd`` for streaming, and
    under ``token_usage.cost`` for non-streaming). This handler aggregates that value per
    model, alongside the standard token usage metadata. ``build_usage_summary`` prefers
    these provider-reported costs over local ``genai_prices`` calculations.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cost_by_model: dict[str, Decimal] = {}
        self._cost_lock = threading.Lock()

    @override
    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        super().on_llm_end(response, **kwargs)
        try:
            generation = response.generations[0][0]
        except IndexError:
            return
        if not isinstance(generation, ChatGeneration):
            return
        message = generation.message
        if not isinstance(message, AIMessage):
            return
        model_name = message.response_metadata.get("model_name")
        if not model_name:
            return
        cost = _extract_provider_cost(message.response_metadata)
        if cost is None:
            return
        with self._cost_lock:
            self.cost_by_model[model_name] = self.cost_by_model.get(model_name, Decimal("0")) + cost


def _extract_provider_cost(response_metadata: dict[str, Any]) -> Decimal | None:
    """Pull the provider-reported cost out of an AIMessage's response_metadata.

    Looks at the streaming-friendly key first (``openrouter_cost_usd``), then falls
    back to the non-streaming ``token_usage.cost`` field exposed by langchain_openai's
    ``llm_output`` propagation. Returns None when no cost is present or when the value
    cannot be coerced to ``Decimal``.
    """
    candidates: list[Any] = []
    if "openrouter_cost_usd" in response_metadata:
        candidates.append(response_metadata["openrouter_cost_usd"])
    token_usage = response_metadata.get("token_usage")
    if isinstance(token_usage, dict) and "cost" in token_usage:
        candidates.append(token_usage["cost"])
    for raw in candidates:
        if raw is None:
            continue
        try:
            return Decimal(str(raw))
        except InvalidOperation, ValueError:
            logger.warning("Could not parse provider cost value %r", raw)
    return None


# Registered once at import time. Upstream ``get_usage_metadata_callback`` creates a new
# ContextVar and hook registration on every call, leaking into the module-level hook list.
_usage_metadata_var: ContextVar[DaivUsageCallbackHandler | None] = ContextVar(
    "daiv_usage_metadata_callback", default=None
)
register_configure_hook(_usage_metadata_var, inheritable=True)


@contextmanager
def track_usage_metadata() -> Iterator[DaivUsageCallbackHandler]:
    """Activate a ``DaivUsageCallbackHandler`` for the enclosed block.

    The handler is auto-propagated to every nested ``Runnable`` invocation (including
    subagents) via the registered ``ContextVar`` hook, so callers don't need to thread
    callbacks through ``RunnableConfig``.
    """
    handler = DaivUsageCallbackHandler()
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


def build_usage_summary(
    handler_data: dict[str, dict[str, Any]], provider_costs: dict[str, Decimal] | None = None
) -> UsageSummary:
    """Build a UsageSummary from ``UsageMetadataCallbackHandler.usage_metadata``.

    When ``provider_costs`` contains an entry for a model, that authoritative cost is
    used (e.g. OpenRouter's ``cost`` field) and the local ``genai_prices`` calculation
    is skipped for that model. Models not in ``provider_costs`` fall back to
    ``genai_prices``.
    """
    if not handler_data:
        logger.warning("Usage metadata is empty; callback hook may not have fired on any LLM call")
        return UsageSummary()

    provider_costs = provider_costs or {}
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

        if (provider_cost := provider_costs.get(model_name)) is not None:
            model_entry["cost_usd"] = str(provider_cost)
            model_entry["cost_source"] = "provider"
            total_cost += provider_cost
        else:
            model_cost = _calc_model_cost(model_name, usage)
            if model_cost is not None:
                model_entry["cost_usd"] = str(model_cost)
                model_entry["cost_source"] = "genai_prices"
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
