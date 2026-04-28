# tests/unit_tests/automation/agent/test_usage_tracking.py
from __future__ import annotations

from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from automation.agent.usage_tracking import DaivUsageCallbackHandler, build_usage_summary


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


class TestBuildUsageSummary:
    def test_single_model(self):
        handler_data = {"claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500)}
        summary = build_usage_summary(handler_data)

        assert summary.input_tokens == 1000
        assert summary.output_tokens == 500
        assert summary.total_tokens == 1500
        assert "claude-sonnet-4-6" in summary.by_model

    def test_multiple_models_aggregated(self):
        handler_data = {
            "claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500),
            "gpt-4o-2024-08-06": _usage_metadata(input_tokens=200, output_tokens=100),
        }
        summary = build_usage_summary(handler_data)

        assert summary.input_tokens == 1200
        assert summary.output_tokens == 600
        assert summary.total_tokens == 1800
        assert len(summary.by_model) == 2

    def test_cost_calculated_for_known_model(self):
        handler_data = {"claude-sonnet-4-6": _usage_metadata(input_tokens=1_000_000, output_tokens=1_000_000)}
        summary = build_usage_summary(handler_data)

        assert summary.cost_usd is not None
        assert Decimal(summary.cost_usd) > Decimal("0")

    def test_cost_none_for_unknown_model(self):
        handler_data = {"totally-unknown-xyz": _usage_metadata(input_tokens=100, output_tokens=50)}
        summary = build_usage_summary(handler_data)

        assert summary.input_tokens == 100
        assert summary.cost_usd is None

    def test_empty_handler_data(self):
        summary = build_usage_summary({})

        assert summary.input_tokens == 0
        assert summary.output_tokens == 0
        assert summary.total_tokens == 0
        assert summary.cost_usd is None
        assert summary.by_model == {}

    def test_cache_tokens_in_by_model(self):
        handler_data = {
            "claude-sonnet-4-6": _usage_metadata(
                input_tokens=1000, output_tokens=500, cache_creation=200, cache_read=300
            )
        }
        summary = build_usage_summary(handler_data)
        model_usage = summary.by_model["claude-sonnet-4-6"]
        assert model_usage["input_token_details"]["cache_creation"] == 200
        assert model_usage["input_token_details"]["cache_read"] == 300

    def test_to_dict_is_json_serializable(self):
        import json

        handler_data = {"claude-sonnet-4-6": _usage_metadata(input_tokens=100, output_tokens=50)}
        d = build_usage_summary(handler_data).to_dict()

        assert json.loads(json.dumps(d)) == d
        assert isinstance(d["input_tokens"], int)
        if d["cost_usd"] is not None:
            assert isinstance(d["cost_usd"], str)

    def test_mixed_known_unknown_models(self):
        """When some models have pricing and some don't, total cost is None."""
        handler_data = {
            "claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500),
            "unknown-model-xyz": _usage_metadata(input_tokens=100, output_tokens=50),
        }
        summary = build_usage_summary(handler_data)

        assert summary.input_tokens == 1100
        assert summary.cost_usd is None
        assert summary.by_model["claude-sonnet-4-6"].get("cost_usd") is not None

    def test_reasoning_tokens_in_by_model(self):
        handler_data = {"claude-sonnet-4-6": _usage_metadata(input_tokens=1000, output_tokens=500, reasoning=200)}
        summary = build_usage_summary(handler_data)
        model_usage = summary.by_model["claude-sonnet-4-6"]
        assert model_usage["output_token_details"]["reasoning"] == 200


class TestProviderCosts:
    def test_provider_cost_overrides_local_calc(self):
        """When the provider reports a cost (e.g. OpenRouter), it wins over genai_prices."""
        handler_data = {
            "anthropic/claude-sonnet-4.6": _usage_metadata(
                input_tokens=1_000_000, output_tokens=1_000_000, cache_creation=100_000
            )
        }
        provider_costs = {"anthropic/claude-sonnet-4.6": Decimal("12.3456")}
        summary = build_usage_summary(handler_data, provider_costs=provider_costs)

        assert summary.cost_usd == "12.3456"
        model_entry = summary.by_model["anthropic/claude-sonnet-4.6"]
        assert model_entry["cost_usd"] == "12.3456"
        assert model_entry["cost_source"] == "provider"

    def test_genai_prices_used_when_provider_cost_missing(self):
        """Models without a provider-reported cost still get a genai_prices estimate."""
        handler_data = {"claude-sonnet-4-6": _usage_metadata(input_tokens=1_000_000, output_tokens=1_000_000)}
        summary = build_usage_summary(handler_data, provider_costs={})

        assert summary.by_model["claude-sonnet-4-6"]["cost_source"] == "genai_prices"
        assert summary.cost_usd is not None

    def test_mixed_provider_and_genai_prices(self):
        """Per-model: prefer provider cost when present, fall back to genai_prices otherwise."""
        handler_data = {
            "anthropic/claude-sonnet-4.6": _usage_metadata(input_tokens=1_000_000, output_tokens=1_000_000),
            "claude-sonnet-4-6": _usage_metadata(input_tokens=1_000_000, output_tokens=1_000_000),
        }
        provider_costs = {"anthropic/claude-sonnet-4.6": Decimal("5.00")}
        summary = build_usage_summary(handler_data, provider_costs=provider_costs)

        provider_model = summary.by_model["anthropic/claude-sonnet-4.6"]
        local_model = summary.by_model["claude-sonnet-4-6"]
        assert provider_model["cost_source"] == "provider"
        assert local_model["cost_source"] == "genai_prices"
        # Aggregated total includes both costs.
        assert summary.cost_usd is not None
        assert Decimal(summary.cost_usd) > Decimal("5.00")


class TestDaivUsageCallbackHandler:
    @staticmethod
    def _llm_result(model_name: str, *, response_metadata: dict[str, Any]) -> LLMResult:
        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            response_metadata={"model_name": model_name, **response_metadata},
        )
        return LLMResult(generations=[[ChatGeneration(message=message)]])

    def test_captures_streaming_cost_from_response_metadata(self):
        """Streaming path: ChatOpenRouter stashes cost as ``openrouter_cost_usd``."""
        handler = DaivUsageCallbackHandler()
        handler.on_llm_end(
            self._llm_result("anthropic/claude-sonnet-4.6", response_metadata={"openrouter_cost_usd": "0.0123"})
        )

        assert handler.cost_by_model == {"anthropic/claude-sonnet-4.6": Decimal("0.0123")}

    def test_captures_non_streaming_cost_from_token_usage(self):
        """Non-streaming path: cost lives at ``response_metadata.token_usage.cost``."""
        handler = DaivUsageCallbackHandler()
        handler.on_llm_end(
            self._llm_result(
                "anthropic/claude-sonnet-4.6",
                response_metadata={"token_usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.045}},
            )
        )

        assert handler.cost_by_model == {"anthropic/claude-sonnet-4.6": Decimal("0.045")}

    def test_aggregates_cost_across_multiple_calls(self):
        handler = DaivUsageCallbackHandler()
        for cost in ("0.10", "0.25"):
            handler.on_llm_end(
                self._llm_result("anthropic/claude-sonnet-4.6", response_metadata={"openrouter_cost_usd": cost})
            )

        assert handler.cost_by_model == {"anthropic/claude-sonnet-4.6": Decimal("0.35")}

    def test_skips_messages_without_cost(self):
        """Direct Anthropic / OpenAI calls won't have OpenRouter's cost — that's fine."""
        handler = DaivUsageCallbackHandler()
        handler.on_llm_end(self._llm_result("claude-sonnet-4-6", response_metadata={}))

        assert handler.cost_by_model == {}


class TestEmptyMetadataWarning:
    def test_empty_handler_data_logs_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="daiv.usage"):
            summary = build_usage_summary({})

        assert summary.input_tokens == 0
        assert any("callback hook may not have fired" in rec.message for rec in caplog.records)
