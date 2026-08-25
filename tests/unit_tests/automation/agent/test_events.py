from automation.agent.events import CONTEXT_USAGE_EVENT, context_usage_payload
from automation.agent.usage_tracking import ResolvedWindow

USAGE = {"input_tokens": 100, "output_tokens": 20, "input_token_details": {"cache_read": 80}}


def _payload(**overrides):
    kwargs = {"model": "claude-sonnet-4-6", "usage": USAGE, "window": ResolvedWindow(1_000_000, "profile")}
    kwargs.update(overrides)
    return context_usage_payload(**kwargs)


def test_the_event_name_is_the_wire_literal():
    assert CONTEXT_USAGE_EVENT == "daiv_context_usage"


def test_the_builder_extracts_the_tokens_and_derives_the_total():
    payload = _payload()

    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 20
    assert payload["cached_tokens"] == 80
    assert payload["used_tokens"] == 120


def test_a_sparse_usage_mapping_defaults_every_count_to_zero():
    payload = _payload(usage={})

    assert payload["input_tokens"] == 0
    assert payload["output_tokens"] == 0
    assert payload["cached_tokens"] == 0
    assert payload["used_tokens"] == 0


def test_a_resolved_window_ships_its_tokens():
    assert _payload(window=ResolvedWindow(200_000, "genai_prices"))["window_tokens"] == 200_000


def test_no_window_ships_null():
    assert _payload(window=None)["window_tokens"] is None


def test_the_key_set_is_the_wire_contract():
    assert set(_payload()) == {
        "model",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "used_tokens",
        "window_tokens",
    }
