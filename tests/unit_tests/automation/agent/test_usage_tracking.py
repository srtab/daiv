# tests/unit_tests/automation/agent/test_usage_tracking.py
from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_openai import ChatOpenAI

from automation.agent.usage_tracking import (
    CostAwareUsageMetadataCallbackHandler,
    _calc_model_cost,
    build_usage_summary,
    message_model_name,
    resolve_context_window,
    resolve_window_by_name,
)
from tests.unit_tests.automation.agent.openai_stream import stream_message


def _usage_metadata(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    total_tokens: int | None = None,
    cache_creation: int = 0,
    cache_read: int = 0,
    ephemeral_5m: int = 0,
    ephemeral_1h: int = 0,
    reasoning: int = 0,
) -> dict[str, Any]:
    """Build a UsageMetadata dict as produced by UsageMetadataCallbackHandler."""
    d: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens if total_tokens is not None else input_tokens + output_tokens,
    }
    input_details: dict[str, int] = {}
    if cache_creation:
        input_details["cache_creation"] = cache_creation
    if cache_read:
        input_details["cache_read"] = cache_read
    if ephemeral_5m:
        input_details["ephemeral_5m_input_tokens"] = ephemeral_5m
    if ephemeral_1h:
        input_details["ephemeral_1h_input_tokens"] = ephemeral_1h
    if input_details:
        d["input_token_details"] = input_details

    output_details: dict[str, int] = {}
    if reasoning:
        output_details["reasoning"] = reasoning
    if output_details:
        d["output_token_details"] = output_details
    return d


def _llm_result(model_name: str, usage: dict[str, Any]) -> LLMResult:
    """Build the LLMResult shape that UsageMetadataCallbackHandler.on_llm_end consumes."""
    message = AIMessage(content="", usage_metadata=usage, response_metadata={"model_name": model_name})
    generation = ChatGeneration(message=message)
    return LLMResult(generations=[[generation]])


def _handler_from_aggregates(aggregates: dict[str, dict[str, Any]]) -> CostAwareUsageMetadataCallbackHandler:
    """Replay each model's aggregate as a single ``on_llm_end`` call.

    Useful for tests that don't care about per-call splitting; for those that do, drive
    ``on_llm_end`` directly with multiple calls.
    """
    handler = CostAwareUsageMetadataCallbackHandler()
    for model_name, usage in aggregates.items():
        handler.on_llm_end(_llm_result(model_name, usage))
    return handler


class TestBuildUsageSummary:
    def test_single_model(self):
        handler = _handler_from_aggregates({"claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500)})
        summary = build_usage_summary(handler)

        assert summary.input_tokens == 1000
        assert summary.output_tokens == 500
        assert summary.total_tokens == 1500
        assert "claude-sonnet-4-6" in summary.by_model

    def test_multiple_models_aggregated(self):
        handler = _handler_from_aggregates({
            "claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500),
            "gpt-4o-2024-08-06": _usage_metadata(input_tokens=200, output_tokens=100),
        })
        summary = build_usage_summary(handler)

        assert summary.input_tokens == 1200
        assert summary.output_tokens == 600
        assert summary.total_tokens == 1800
        assert len(summary.by_model) == 2

    def test_cost_calculated_for_known_model(self):
        handler = _handler_from_aggregates({
            "claude-sonnet-4-6": _usage_metadata(input_tokens=1_000_000, output_tokens=1_000_000)
        })
        summary = build_usage_summary(handler)

        assert summary.cost_usd is not None
        assert Decimal(summary.cost_usd) > Decimal("0")

    def test_cost_none_for_unknown_model(self):
        handler = _handler_from_aggregates({"totally-unknown-xyz": _usage_metadata(input_tokens=100, output_tokens=50)})
        summary = build_usage_summary(handler)

        assert summary.input_tokens == 100
        assert summary.cost_usd is None

    def test_empty_handler_data(self):
        handler = CostAwareUsageMetadataCallbackHandler()
        summary = build_usage_summary(handler)

        assert summary.input_tokens == 0
        assert summary.output_tokens == 0
        assert summary.total_tokens == 0
        assert summary.cost_usd is None
        assert summary.by_model == {}

    def test_cache_tokens_in_by_model(self):
        handler = _handler_from_aggregates({
            "claude-sonnet-4-6": _usage_metadata(
                input_tokens=1000, output_tokens=500, cache_creation=200, cache_read=300
            )
        })
        summary = build_usage_summary(handler)
        model_usage = summary.by_model["claude-sonnet-4-6"]
        assert model_usage["input_token_details"]["cache_creation"] == 200
        assert model_usage["input_token_details"]["cache_read"] == 300

    def test_to_dict_is_json_serializable(self):
        import json

        handler = _handler_from_aggregates({"claude-sonnet-4-6": _usage_metadata(input_tokens=100, output_tokens=50)})
        d = build_usage_summary(handler).to_dict()

        assert json.loads(json.dumps(d)) == d
        assert isinstance(d["input_tokens"], int)
        if d["cost_usd"] is not None:
            assert isinstance(d["cost_usd"], str)

    def test_mixed_known_unknown_models(self):
        """When some models have pricing and some don't, total cost is None."""
        handler = _handler_from_aggregates({
            "claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500),
            "unknown-model-xyz": _usage_metadata(input_tokens=100, output_tokens=50),
        })
        summary = build_usage_summary(handler)

        assert summary.input_tokens == 1100
        assert summary.cost_usd is None
        assert summary.by_model["claude-sonnet-4-6"].get("cost_usd") is not None

    def test_reasoning_tokens_in_by_model(self):
        handler = _handler_from_aggregates({
            "claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500, reasoning=200)
        })
        summary = build_usage_summary(handler)
        model_usage = summary.by_model["claude-sonnet-4-6"]
        assert model_usage["output_token_details"]["reasoning"] == 200


class TestPerCallPricing:
    """Per-call pricing must avoid the tier-rate trap (regression for cost over-charge)."""

    def test_two_subthreshold_calls_use_base_rate(self):
        """Two 150K-input gpt-5.4 calls aggregate to 300K (above the 272K tier threshold).

        Pricing each call individually keeps both at the base rate; pricing the aggregate
        would incorrectly apply the tier rate to all 300K tokens. Assert the per-call sum
        matches the base-rate calculation.
        """
        from genai_prices import Usage, calc_price

        per_call_input = 150_000
        per_call_output = 1_000

        handler = CostAwareUsageMetadataCallbackHandler()
        for _ in range(2):
            handler.on_llm_end(
                _llm_result("gpt-5.4", _usage_metadata(input_tokens=per_call_input, output_tokens=per_call_output))
            )

        summary = build_usage_summary(handler)

        # Reference value: sum of two independently priced calls.
        expected = Decimal("0")
        for _ in range(2):
            r = calc_price(Usage(input_tokens=per_call_input, output_tokens=per_call_output), model_ref="gpt-5.4")
            expected += r.total_price

        assert summary.cost_usd is not None
        assert Decimal(summary.cost_usd) == expected
        # Aggregate token totals are still summed.
        assert summary.input_tokens == per_call_input * 2
        assert summary.output_tokens == per_call_output * 2

        # Sanity: confirm aggregate-then-price would have produced a strictly larger cost.
        aggregate = calc_price(
            Usage(input_tokens=per_call_input * 2, output_tokens=per_call_output * 2), model_ref="gpt-5.4"
        )
        assert aggregate.total_price > expected

    def test_single_call_above_threshold_uses_tier_rate(self):
        """A genuine single call above the tier threshold should still use the tier rate."""
        from genai_prices import Usage, calc_price

        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(_llm_result("gpt-5.4", _usage_metadata(input_tokens=500_000, output_tokens=1_000)))

        summary = build_usage_summary(handler)
        expected = calc_price(Usage(input_tokens=500_000, output_tokens=1_000), model_ref="gpt-5.4").total_price

        assert summary.cost_usd is not None
        assert Decimal(summary.cost_usd) == expected

    def test_call_without_usage_metadata_is_skipped(self):
        """LLMResults without usage_metadata or model_name don't crash and don't pollute totals."""
        handler = CostAwareUsageMetadataCallbackHandler()
        # Generation lacking AIMessage.usage_metadata
        bad_msg = AIMessage(content="", response_metadata={"model_name": "claude-sonnet-4-6"})
        bad_result = LLMResult(generations=[[ChatGeneration(message=bad_msg)]])
        handler.on_llm_end(bad_result)

        # Generation lacking model_name
        msg_no_model = AIMessage(content="", usage_metadata=_usage_metadata(input_tokens=10, output_tokens=5))
        result_no_model = LLMResult(generations=[[ChatGeneration(message=msg_no_model)]])
        handler.on_llm_end(result_no_model)

        summary = build_usage_summary(handler)
        assert summary.input_tokens == 0
        assert summary.cost_usd is None

    def test_unknown_model_disables_pricing_for_run(self):
        """A single unpriced call leaves cost_usd = None even if other calls were priced."""
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(_llm_result("claude-sonnet-4-6", _usage_metadata(input_tokens=1000, output_tokens=500)))
        handler.on_llm_end(_llm_result("totally-unknown-xyz", _usage_metadata(input_tokens=100, output_tokens=50)))

        summary = build_usage_summary(handler)
        assert summary.cost_usd is None
        # Per-model cost still recorded for the priced model.
        assert summary.by_model["claude-sonnet-4-6"].get("cost_usd") is not None
        assert summary.by_model["totally-unknown-xyz"].get("cost_usd") is None

    def test_cost_accumulates_across_calls_for_same_model(self):
        """Multiple calls for the same model add up to the per-model cost in by_model."""
        handler = CostAwareUsageMetadataCallbackHandler()
        for _ in range(3):
            handler.on_llm_end(_llm_result("claude-sonnet-4-6", _usage_metadata(input_tokens=1_000, output_tokens=500)))

        summary = build_usage_summary(handler)
        assert summary.cost_usd is not None
        # by_model.cost_usd should equal the run total (only one model).
        assert summary.by_model["claude-sonnet-4-6"]["cost_usd"] == summary.cost_usd


class TestEphemeralCacheWrites:
    """langchain-anthropic stores 1h/5m cache-write counts under ``ephemeral_*_input_tokens``
    and zeroes ``cache_creation`` when that breakdown is present. Per-TTL counts must be
    billed correctly and the 1h surcharge applied (Anthropic prices 1h writes at 2x base,
    while genai-prices only models the 5m rate).
    """

    def test_reproduces_langsmith_cost_for_known_trace(self):
        """Trace ``019df81d`` has 414953/2029 tokens with 308360 cache_read and 106570
        ephemeral_1h cache writes; LangSmith reports total $0.762432.
        """
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                "claude-sonnet-4-6",
                _usage_metadata(input_tokens=414953, output_tokens=2029, cache_read=308360, ephemeral_1h=106570),
            )
        )
        summary = build_usage_summary(handler)
        assert summary.cost_usd is not None
        # Reconstruct the LangSmith formula exactly:
        # uncached*$3 + cache_read*$0.30 + ephemeral_1h*$6 + output*$15  (all per-million).
        assert Decimal(summary.cost_usd) == Decimal("0.762432")

    def test_ephemeral_5m_billed_at_5m_rate(self):
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                "claude-sonnet-4-6", _usage_metadata(input_tokens=100_000, output_tokens=0, ephemeral_5m=100_000)
            )
        )
        summary = build_usage_summary(handler)
        assert summary.cost_usd is not None
        # 100K tokens entirely as 5m cache writes: 100_000 * $3.75/M = $0.375.
        assert Decimal(summary.cost_usd) == Decimal("0.375")

    def test_ephemeral_1h_priced_higher_than_5m(self):
        """1h cache writes cost strictly more than the same volume as 5m writes."""

        def _cost(*, ephemeral_1h: int, ephemeral_5m: int) -> Decimal:
            handler = CostAwareUsageMetadataCallbackHandler()
            handler.on_llm_end(
                _llm_result(
                    "claude-sonnet-4-6",
                    _usage_metadata(
                        input_tokens=100_000, output_tokens=0, ephemeral_5m=ephemeral_5m, ephemeral_1h=ephemeral_1h
                    ),
                )
            )
            summary = build_usage_summary(handler)
            assert summary.cost_usd is not None
            return Decimal(summary.cost_usd)

        cost_1h = _cost(ephemeral_1h=100_000, ephemeral_5m=0)
        cost_5m = _cost(ephemeral_1h=0, ephemeral_5m=100_000)
        # 1h surcharge for Sonnet 4.6 is $2.25/M (= 2*$3 − $3.75); for 100K tokens that's $0.225.
        assert cost_1h - cost_5m == Decimal("0.225")

    def test_legacy_cache_creation_still_priced(self):
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                "claude-sonnet-4-6", _usage_metadata(input_tokens=100_000, output_tokens=0, cache_creation=100_000)
            )
        )
        summary = build_usage_summary(handler)
        # Same as the 5m case — no surcharge, just the genai-prices cache_write rate.
        assert summary.cost_usd is not None
        assert Decimal(summary.cost_usd) == Decimal("0.375")

    def test_surcharge_skipped_for_non_anthropic_provider(self):
        """The 1h surcharge is Anthropic-specific. A non-Anthropic model with the same
        input_token_details shape must price exactly as genai-prices reports, no surcharge.
        """
        from genai_prices import Usage, calc_price

        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(
            _llm_result("gpt-5.4", _usage_metadata(input_tokens=100_000, output_tokens=0, ephemeral_1h=10_000))
        )
        summary = build_usage_summary(handler)
        expected = calc_price(
            Usage(input_tokens=100_000, output_tokens=0, cache_write_tokens=10_000), model_ref="gpt-5.4"
        ).total_price
        assert summary.cost_usd is not None
        assert Decimal(summary.cost_usd) == expected

    def test_surcharge_uses_tiered_rate_above_200k(self):
        """Sonnet 4.5 has TieredPrices crossing at 200K. Above the threshold the surcharge
        must use the tier rate (2*$6 − $7.50 = $4.50/M), not the base rate.
        """
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                "claude-sonnet-4-5", _usage_metadata(input_tokens=250_000, output_tokens=0, ephemeral_1h=10_000)
            )
        )
        summary = build_usage_summary(handler)
        assert summary.cost_usd is not None
        # genai-prices base cost (tier rate, all tokens billed as cache_write since
        # cache_write_tokens=ephemeral_1h=10_000 and the rest is uncached input):
        # uncached = 250_000 − 10_000 = 240_000 @ $6/M = $1.440
        # cache_write = 10_000 @ $7.50/M = $0.075
        # surcharge = 10_000 * (2*$6 − $7.50)/M = $0.045
        assert Decimal(summary.cost_usd) == Decimal("1.560")

    def test_openrouter_routed_anthropic_gets_surcharge(self):
        """Routing Anthropic via OpenRouter (model_name like ``anthropic/claude-sonnet-4.6``)
        resolves to provider ``openrouter`` but underlying pricing is Anthropic's, so the
        1h surcharge must still apply.
        """
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                "anthropic/claude-sonnet-4.6",
                _usage_metadata(input_tokens=100_000, output_tokens=0, ephemeral_1h=100_000),
            )
        )
        summary = build_usage_summary(handler)
        assert summary.cost_usd is not None
        # Same shape as the 1h-vs-5m delta test: 100K @ ($3.75/M base + $2.25/M surcharge) = $0.600.
        assert Decimal(summary.cost_usd) == Decimal("0.600")

    def test_mixed_cache_fields_sum_into_cache_writes(self):
        """``cache_creation``, ``ephemeral_5m_input_tokens`` and ``ephemeral_1h_input_tokens``
        all contribute to ``cache_write_tokens``; the surcharge applies only to the 1h slice.
        """
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                "claude-sonnet-4-6",
                _usage_metadata(
                    input_tokens=100_000,
                    output_tokens=0,
                    cache_creation=20_000,
                    ephemeral_5m=30_000,
                    ephemeral_1h=50_000,
                ),
            )
        )
        summary = build_usage_summary(handler)
        assert summary.cost_usd is not None
        # All 100K billed as cache_write at $3.75/M = $0.375, plus 50K ephemeral_1h surcharge
        # at $2.25/M = $0.1125 → $0.4875.
        assert Decimal(summary.cost_usd) == Decimal("0.4875")


class TestEmptyMetadataWarning:
    def test_empty_handler_data_logs_warning(self, caplog):
        import logging

        handler = CostAwareUsageMetadataCallbackHandler()
        with caplog.at_level(logging.WARNING, logger="daiv.usage"):
            summary = build_usage_summary(handler)

        assert summary.input_tokens == 0
        assert any("callback hook may not have fired" in rec.message for rec in caplog.records)


def _stream_message(model: str, *, finish_chunks: int) -> AIMessage:
    """Accumulate a doubling stream through a *stock* ``ChatOpenAI``.

    Stock, not ``ChatOpenRouter``: that subclass dedupes the keys at the source, so it can no
    longer produce the corruption this repair exists for. A plain ``ChatOpenAI`` is exactly the
    remaining exposure — an OpenAI-compatible gateway configured as ``provider_type=OPENAI``
    reaches one through ``init_chat_model`` and never passes through the subclass.
    """
    return stream_message(ChatOpenAI(model=model, api_key="test"), finish_chunks=finish_chunks, model=model)


def _priced_message(*, finish_chunks: int) -> AIMessage:
    """A streamed message on a priceable model, carrying usage the handler can aggregate."""
    message = _stream_message("anthropic/claude-sonnet-4.6", finish_chunks=finish_chunks)
    message.usage_metadata = _usage_metadata(input_tokens=1000, output_tokens=500)
    return message


class TestStreamedModelName:
    """The read-side repair, over the gateway path that has no source-level fix — see the
    ``automation.agent.chat_models`` module docstring for the mechanism, and
    ``TestChatOpenRouterRestampedMetadata`` for the source-level half."""

    def test_a_doubled_name_prices_and_resolves_its_window_once_repaired(self):
        """The point of the repair, driven end to end because the doubling comes from upstream's
        own chunk merge: a fixture asserting the doubled literal would keep passing after
        upstream stopped producing it. Both lookups are by name, and both miss on that form."""
        doubled = _stream_message("anthropic/claude-sonnet-4.6", finish_chunks=2)
        raw = doubled.response_metadata["model_name"]

        assert resolve_window_by_name(raw) is None
        assert _calc_model_cost(raw, {"input_tokens": 10, "output_tokens": 5}) is None
        repaired = message_model_name(doubled)
        assert repaired == "anthropic/claude-sonnet-4.6"
        assert resolve_window_by_name(repaired) is not None
        assert _calc_model_cost(repaired, {"input_tokens": 10, "output_tokens": 5}) is not None

    def test_an_undoubled_stream_is_left_alone(self):
        assert message_model_name(_stream_message("z-ai/glm-5.2", finish_chunks=1)) == "z-ai/glm-5.2"

    def test_three_finish_chunks_collapse_too(self):
        """Period ≠ half the length — the case a "does the first half equal the second" check
        would miss."""
        assert message_model_name(_stream_message("gpt-5.4", finish_chunks=3)) == "gpt-5.4"

    def test_the_usage_handler_keys_by_the_repaired_name(self):
        """``usage_by_model`` keys reach the spend sheet verbatim, so the repair has to land
        before aggregation, not in the template."""
        handler = CostAwareUsageMetadataCallbackHandler()
        handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=_priced_message(finish_chunks=2))]]))

        summary = build_usage_summary(handler)

        assert list(summary.by_model) == ["anthropic/claude-sonnet-4.6"]
        assert summary.cost_usd is not None

    def test_a_doubled_and_a_clean_turn_on_one_model_land_in_a_single_row(self):
        """The repair happens before accumulation, so a streamed turn and a side-run on the same
        model add up rather than splitting the row — and neither loses its tokens.
        """
        handler = CostAwareUsageMetadataCallbackHandler()
        for finish_chunks in (2, 1):  # the streamed turn, then a side-run on the same model
            handler.on_llm_end(
                LLMResult(generations=[[ChatGeneration(message=_priced_message(finish_chunks=finish_chunks))]])
            )

        summary = build_usage_summary(handler)

        assert list(summary.by_model) == ["anthropic/claude-sonnet-4.6"]
        assert summary.by_model["anthropic/claude-sonnet-4.6"]["input_tokens"] == 2000
        assert summary.input_tokens == 2000

    def test_a_message_with_no_model_name_is_none(self):
        assert message_model_name(AIMessage(content="")) is None

    @pytest.mark.parametrize(
        "name",
        [
            "claude-sonnet-4-6",
            # Not a whole-string repeat: repairing it would invent a model nobody ran.
            "z-ai/glm-5.2anthropic/claude-sonnet-4.6",
            # A repeated *substring* is not a repeated string.
            "deepseek/deepseek-v4-flash",
        ],
    )
    def test_names_that_are_not_whole_repeats_are_left_alone(self, name):
        message = AIMessage(content="", response_metadata={"model_name": name})

        assert message_model_name(message) == name

    @pytest.mark.parametrize(
        "name",
        [
            "aa",
            "1.51.5",
            "gg",
            "--",
            "4.14.1",
            # Repeated more than twice: the floor screens the *minimal* period, so these stay
            # whole rather than collapsing to the equally-invented "1.51.5" / "aaaa".
            "1.51.51.51.5",
            "aaaaaaaa",
        ],
    )
    def test_a_repeat_too_short_to_be_a_model_is_left_alone(self, name):
        """No real name is shorter than the floor, so a short period is a coincidence rather
        than a doubling — and halving one invents a model nobody ran, which then misses every
        lookup exactly as the doubled name did.
        """
        message = AIMessage(content="", response_metadata={"model_name": name})

        assert message_model_name(message) == name


def _model(profile=None) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([]), profile=profile)


class TestResolveContextWindow:
    def test_a_profile_value_wins_over_genai_prices(self):
        resolved = resolve_context_window(_model({"max_input_tokens": 777}), "anthropic/claude-sonnet-4.6")

        assert resolved == (777, "profile")

    def test_an_empty_profile_falls_through_instead_of_resolving_to_zero(self):
        """ChatGoogleGenerativeAI ships ``{}``; gating on the profile's *presence* would
        resolve it as tier 1 with no tokens. The check is on the value."""
        resolved = resolve_context_window(_model({}), "anthropic/claude-sonnet-4.6")

        assert resolved is not None
        assert resolved.source == "genai_prices"

    def test_a_profile_less_model_resolves_from_genai_prices(self):
        assert resolve_context_window(_model(None), "anthropic/claude-sonnet-4.6") == (1_000_000, "genai_prices")

    def test_an_unknown_model_returns_none(self):
        assert resolve_context_window(_model(None), "model-that-does-not-exist-xyz") is None

    def test_the_price_path_and_the_window_path_infer_the_same_provider(self):
        """Both must resolve the slash form via the shared provider-inference helper —
        one succeeding while the other LookupErrors means the rule was re-enumerated."""
        assert resolve_window_by_name("anthropic/claude-sonnet-4.6") is not None
        assert _calc_model_cost("anthropic/claude-sonnet-4.6", {"input_tokens": 10, "output_tokens": 5}) is not None

    def test_every_hit_carries_its_source_tier(self):
        assert resolve_context_window(_model({"max_input_tokens": 100}), "x").source == "profile"
        assert resolve_context_window(_model(None), "anthropic/claude-sonnet-4.6").source == "genai_prices"

    def test_the_cache_never_serves_tier_one(self):
        """Two instances of one model name with different profiles resolve independently:
        a name-keyed cache over the whole resolver would freeze the first configuration
        process-wide (the context-1m beta raises ``max_input_tokens`` per instance)."""
        a = resolve_context_window(_model({"max_input_tokens": 200_000}), "claude-sonnet-4-6")
        b = resolve_context_window(_model({"max_input_tokens": 1_000_000}), "claude-sonnet-4-6")

        assert (a.tokens, b.tokens) == (200_000, 1_000_000)

    def test_a_zero_snapshot_window_normalizes_to_none(self):
        """genai-prices models ``context_window`` as ``int | None`` with 0 representable;
        the normalization lives here, once, so no caller can ship a zero window."""
        with mock.patch("automation.agent.usage_tracking.data_snapshot") as snapshot:
            snapshot.get_snapshot.return_value.find_provider_model.return_value = (None, mock.Mock(context_window=0))
            assert resolve_window_by_name("zero-window-model") is None

    def test_the_name_only_resolver_is_tier_two_with_its_label(self):
        assert resolve_window_by_name("anthropic/claude-sonnet-4.6") == (1_000_000, "genai_prices")
        assert resolve_window_by_name("model-that-does-not-exist-xyz") is None
