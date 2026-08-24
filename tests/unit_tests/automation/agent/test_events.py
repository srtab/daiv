from automation.agent.events import CONTEXT_USAGE_EVENT, context_usage_payload


def _payload(**overrides):
    kwargs = {
        "model": "claude-sonnet-4-6",
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": 80,
        "window_tokens": 1_000_000,
        "window_source": "profile",
    }
    kwargs.update(overrides)
    return context_usage_payload(**kwargs)


def test_the_event_name_is_the_wire_literal():
    assert CONTEXT_USAGE_EVENT == "daiv_context_usage"


def test_the_builder_derives_used_tokens():
    assert _payload(input_tokens=100, output_tokens=20)["used_tokens"] == 120


def test_window_source_is_null_exactly_when_window_tokens_is():
    payload = _payload(window_tokens=None, window_source="genai_prices")

    assert payload["window_tokens"] is None
    assert payload["window_source"] is None


def test_the_key_set_is_the_wire_contract():
    assert set(_payload()) == {
        "model",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "used_tokens",
        "window_tokens",
        "window_source",
    }
