# tests/unit_tests/automation/agent/test_usage_tracking.py
from __future__ import annotations

from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from automation.agent.usage_tracking import CostAwareUsageMetadataCallbackHandler, build_usage_summary


def _usage_metadata(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    total_tokens: int | None = None,
    cache_creation: int = 0,
    cache_read: int = 0,
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


class TestEmptyMetadataWarning:
    def test_empty_handler_data_logs_warning(self, caplog):
        import logging

        handler = CostAwareUsageMetadataCallbackHandler()
        with caplog.at_level(logging.WARNING, logger="daiv.usage"):
            summary = build_usage_summary(handler)

        assert summary.input_tokens == 0
        assert any("callback hook may not have fired" in rec.message for rec in caplog.records)
