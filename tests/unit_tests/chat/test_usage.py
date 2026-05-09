from __future__ import annotations

from chat.usage import cache_token_totals, get_context_window


def test_get_context_window_known_anthropic_model():
    window = get_context_window("claude-sonnet-4-6")
    assert window is not None
    assert window >= 100_000


def test_get_context_window_unknown_model_returns_none():
    assert get_context_window("totally-fake-model-name-xyz") is None


def test_get_context_window_openrouter_prefix_returns_none():
    # LangChain's profile registry doesn't recognise OpenRouter-prefixed names.
    # The UI relies on this returning None to omit the % indicator gracefully.
    assert get_context_window("openrouter/anthropic/claude-sonnet-4.6") is None


def test_get_context_window_none_or_empty_returns_none():
    assert get_context_window(None) is None
    assert get_context_window("") is None


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
