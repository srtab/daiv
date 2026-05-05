from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from genai_prices import Usage, calc_price
from genai_prices.types import TieredPrices
from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.tracers.context import register_configure_hook

from automation.agent.base import ModelProvider

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from langchain_core.outputs import LLMResult

logger = logging.getLogger("daiv.usage")


def _resolve_mtok_rate(field_mtok: Decimal | TieredPrices | None, total_input_tokens: int) -> Decimal | None:
    """Resolve the per-million-token rate that applies at ``total_input_tokens``.

    Mirrors ``genai_prices.types.calc_mtok_price`` tier selection.
    """
    if field_mtok is None:
        return None
    if isinstance(field_mtok, TieredPrices):
        applicable = field_mtok.base
        for tier in reversed(field_mtok.tiers):
            if total_input_tokens > tier.start:
                applicable = tier.price
                break
        return applicable
    return field_mtok


def _calc_model_cost(model_name: str, usage_metadata: Mapping[str, Any]) -> Decimal | None:
    """Return None if the model is not in the pricing database."""
    input_details = usage_metadata.get("input_token_details") or {}
    # langchain-anthropic zeroes ``cache_creation`` whenever it has the per-TTL breakdown
    # and stores the actual counts under ``ephemeral_5m_input_tokens`` /
    # ``ephemeral_1h_input_tokens`` (see ``_create_usage_metadata`` in chat_models.py).
    # Sum all three so we don't silently drop the cache-write portion of input.
    ephemeral_5m = input_details.get("ephemeral_5m_input_tokens") or 0
    ephemeral_1h = input_details.get("ephemeral_1h_input_tokens") or 0
    cache_write_tokens = (input_details.get("cache_creation") or 0) + ephemeral_5m + ephemeral_1h

    input_tokens = usage_metadata.get("input_tokens", 0)
    genai_usage = Usage(
        input_tokens=input_tokens,
        output_tokens=usage_metadata.get("output_tokens", 0),
        cache_write_tokens=cache_write_tokens,
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
    except ValueError, TypeError, ArithmeticError:
        logger.warning("Cost calculation failed for model %r", model_name, exc_info=True)
        return None

    cost = result.total_price

    # Anthropic prices 1-hour ephemeral cache writes at 2x the base input rate, while
    # 5-minute writes are 1.25x. genai-prices' single ``cache_write_mtok`` reflects only
    # the 5-minute rate, so add the (1h − 5m) differential for the 1h portion.
    # OpenRouter-routed Anthropic models share the underlying rates and need the same surcharge.
    is_anthropic = result.provider.id == ModelProvider.ANTHROPIC or model_name.startswith(
        f"{ModelProvider.ANTHROPIC.value}/"
    )
    if ephemeral_1h and is_anthropic:
        input_rate = _resolve_mtok_rate(result.model_price.input_mtok, input_tokens)
        cache_write_rate = _resolve_mtok_rate(result.model_price.cache_write_mtok, input_tokens)
        if input_rate is not None and cache_write_rate is not None:
            surcharge_per_mtok = Decimal(2) * input_rate - cache_write_rate
            cost += Decimal(ephemeral_1h) * surcharge_per_mtok / Decimal(1_000_000)
        else:
            # Pricing entry is missing a rate (e.g. older Claude models with no cache pricing).
            # Surface the under-billing so it doesn't slip past silently.
            logger.warning(
                "1h cache-write surcharge skipped for %r: input_rate=%s cache_write_rate=%s; "
                "%d ephemeral_1h tokens billed at the 5m rate",
                model_name,
                input_rate,
                cache_write_rate,
                ephemeral_1h,
            )

    return cost


class CostAwareUsageMetadataCallbackHandler(UsageMetadataCallbackHandler):
    """Token-usage callback that also accumulates cost **per LLM call**.

    The stock ``UsageMetadataCallbackHandler`` only retains aggregated usage per model.
    Pricing the aggregate is wrong for tier-priced models (e.g. gpt-5.4 doubles its
    rate above 272K input): individual API calls stay below the threshold and bill at
    base rate, but the aggregate crosses it and gets the tier rate applied to every
    token. We override ``on_llm_end`` to price each call individually and sum.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cost_by_model: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        # Sticky ``False`` once any call for the model yields no price (unknown model, etc.).
        self.priced_by_model: dict[str, bool] = {}

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        super().on_llm_end(response, **kwargs)

        try:
            generation = response.generations[0][0]
        except IndexError:
            return

        if not isinstance(generation, ChatGeneration) or not isinstance(generation.message, AIMessage):
            return

        message = generation.message
        usage_metadata = message.usage_metadata
        model_name = message.response_metadata.get("model_name")
        if not usage_metadata or not model_name:
            return

        # Skip the pricing lookup (and its per-call warning) for models we already know are unpriced.
        if self.priced_by_model.get(model_name) is False:
            return

        cost = _calc_model_cost(model_name, usage_metadata)

        with self._lock:
            if cost is None:
                self.priced_by_model[model_name] = False
                return
            self.priced_by_model.setdefault(model_name, True)
            self.cost_by_model[model_name] += cost


# Registered once at import time. Upstream ``get_usage_metadata_callback`` creates a new
# ContextVar and hook registration on every call, leaking into the module-level hook list.
_usage_metadata_var: ContextVar[CostAwareUsageMetadataCallbackHandler | None] = ContextVar(
    "daiv_usage_metadata_callback", default=None
)
register_configure_hook(_usage_metadata_var, inheritable=True)


@contextmanager
def track_usage_metadata() -> Iterator[CostAwareUsageMetadataCallbackHandler]:
    """Activate a ``CostAwareUsageMetadataCallbackHandler`` for the enclosed block.

    The handler is auto-propagated to every nested ``Runnable`` invocation (including
    subagents) via the registered ``ContextVar`` hook, so callers don't need to thread
    callbacks through ``RunnableConfig``.
    """
    handler = CostAwareUsageMetadataCallbackHandler()
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


def build_usage_summary(handler: CostAwareUsageMetadataCallbackHandler) -> UsageSummary:
    """Build a UsageSummary from a ``CostAwareUsageMetadataCallbackHandler``."""
    handler_data = handler.usage_metadata
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

        if handler.priced_by_model.get(model_name):
            model_cost = handler.cost_by_model[model_name]
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
