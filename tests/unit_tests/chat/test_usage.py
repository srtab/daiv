from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from chat.usage import (
    _CONTEXT_WINDOW_CACHE,
    USAGE_SUMMARY_EVENT_NAME,
    build_thread_usage_payload,
    cache_token_totals,
    get_context_window,
)


@pytest.fixture(autouse=True)
def _clear_context_window_cache():
    _CONTEXT_WINDOW_CACHE.clear()
    yield
    _CONTEXT_WINDOW_CACHE.clear()


def test_get_context_window_returns_profile_max_input_tokens():
    fake_model = MagicMock()
    fake_model.profile = {"max_input_tokens": 200_000}
    with patch("chat.usage.BaseAgent.get_model", return_value=fake_model) as get_model:
        assert get_context_window("claude-sonnet-4-6") == 200_000
    get_model.assert_called_once_with(model="claude-sonnet-4-6")


def test_get_context_window_caches_successful_lookups():
    fake_model = MagicMock()
    fake_model.profile = {"max_input_tokens": 200_000}
    with patch("chat.usage.BaseAgent.get_model", return_value=fake_model) as get_model:
        assert get_context_window("claude-sonnet-4-6") == 200_000
        assert get_context_window("claude-sonnet-4-6") == 200_000
    assert get_model.call_count == 1


def test_get_context_window_does_not_cache_failures():
    """A transient lookup failure must not poison the cache for the rest of the
    process — the next call retries.
    """
    with patch("chat.usage.BaseAgent.get_model", side_effect=Exception("transient")) as get_model:
        assert get_context_window("model-x") is None
        assert get_context_window("model-x") is None
    assert get_model.call_count == 2


def test_get_context_window_unknown_model_returns_none():
    with patch("chat.usage.BaseAgent.get_model", side_effect=Exception("unknown model")):
        assert get_context_window("totally-fake-model-name-xyz") is None


def test_get_context_window_empty_profile_returns_none():
    fake_model = MagicMock()
    fake_model.profile = {}
    with patch("chat.usage.BaseAgent.get_model", return_value=fake_model):
        assert get_context_window("openrouter/anthropic/claude-sonnet-4.6") is None


def test_get_context_window_none_or_empty_returns_none():
    assert get_context_window(None) is None
    assert get_context_window("") is None


def test_build_thread_usage_payload_none_returns_zeroed_shape():
    payload = build_thread_usage_payload(None)
    assert payload == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": None,
        "by_model": {},
        "last_model_name": None,
        "last_input_tokens": 0,
        "context_window": None,
    }


def test_build_thread_usage_payload_none_returns_independent_dict():
    """Each call must return a fresh dict — mutating one must not leak to another."""
    a = build_thread_usage_payload(None)
    a["by_model"]["leaked"] = 1
    b = build_thread_usage_payload(None)
    assert b["by_model"] == {}


def test_build_thread_usage_payload_populated_thread():
    thread = SimpleNamespace(
        input_tokens=12345,
        output_tokens=678,
        total_tokens=13023,
        cache_read_tokens=4096,
        cache_write_tokens=1024,
        cost_usd=Decimal("0.012345"),
        cost_priced=True,
        usage_by_model={"m": {"input_tokens": 12345}},
        last_model_name="m",
        last_input_tokens=2345,
    )
    with patch("chat.usage.get_context_window", return_value=200_000):
        payload = build_thread_usage_payload(thread)

    assert payload["input_tokens"] == 12345
    assert payload["output_tokens"] == 678
    assert payload["total_tokens"] == 13023
    assert payload["cache_read_tokens"] == 4096
    assert payload["cache_write_tokens"] == 1024
    assert payload["cost_usd"] == "0.012345"
    assert payload["last_model_name"] == "m"
    assert payload["last_input_tokens"] == 2345
    assert payload["context_window"] == 200_000


def test_build_thread_usage_payload_unpriced_emits_null_cost():
    thread = SimpleNamespace(
        input_tokens=10,
        output_tokens=0,
        total_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=None,
        cost_priced=False,
        usage_by_model=None,
        last_model_name="",
        last_input_tokens=0,
    )
    with patch("chat.usage.get_context_window", return_value=None):
        payload = build_thread_usage_payload(thread)

    assert payload["cost_usd"] is None
    assert payload["by_model"] == {}
    assert payload["last_model_name"] is None


def test_build_thread_usage_payload_priced_with_null_cost_still_emits_null():
    """cost_priced=True but cost_usd=None (first delta hasn't landed) → emit None."""
    thread = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=None,
        cost_priced=True,
        usage_by_model={},
        last_model_name=None,
        last_input_tokens=0,
    )
    with patch("chat.usage.get_context_window", return_value=None):
        payload = build_thread_usage_payload(thread)
    assert payload["cost_usd"] is None


def test_usage_summary_event_name_constant():
    assert USAGE_SUMMARY_EVENT_NAME == "daiv.usage_summary"


def test_cache_token_totals_empty():
    assert cache_token_totals({}) == (0, 0)


def test_cache_token_totals_sums_read_and_write():
    by_model = {
        "m1": {
            "input_token_details": {
                "cache_read": 100,
                "cache_creation": 5,
                "ephemeral_5m_input_tokens": 10,
                "ephemeral_1h_input_tokens": 20,
            }
        },
        "m2": {
            "input_token_details": {
                "cache_read": 200
                # no cache_creation / ephemeral_*
            }
        },
        "m3": {},  # no input_token_details
    }
    read, write = cache_token_totals(by_model)
    assert read == 300
    assert write == 35  # 5 + 10 + 20 from m1, 0 from others
