"""``derive_context_usage`` — the reload half of the context meter (design §4/§2)."""

from langchain_core.messages import AIMessage, HumanMessage
from sessions.hydration import derive_context_usage

from automation.agent.events import context_usage_payload

USAGE = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "input_token_details": {"cache_read": 60}}


def _ai(usage=None, model=None, content="ok") -> AIMessage:
    kwargs = {}
    if usage is not None:
        kwargs["usage_metadata"] = usage
    if model is not None:
        kwargs["response_metadata"] = {"model_name": model}
    return AIMessage(content=content, **kwargs)


def test_the_seed_comes_from_the_last_ai_message_carrying_usage():
    """A middleware-minted reply (no usage) and the trailing user turn are skipped — the
    reading is the last model call, same as the live event."""
    older = _ai(usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}, model="anthropic/claude-sonnet-4.6")
    latest = _ai(usage=USAGE, model="anthropic/claude-sonnet-4.6")

    seed = derive_context_usage([older, latest, _ai(), HumanMessage(content="hi")])

    assert seed["used_tokens"] == 110
    assert seed["cached_tokens"] == 60
    assert seed["window_tokens"] == 1_000_000
    assert seed["window_source"] == "genai_prices"


def test_no_usage_anywhere_yields_no_seed():
    assert derive_context_usage([HumanMessage(content="hi"), _ai()]) is None


def test_an_unknown_model_seeds_the_count_without_a_window():
    seed = derive_context_usage([_ai(usage=USAGE, model="model-nobody-knows")])

    assert seed["used_tokens"] == 110
    assert seed["window_tokens"] is None
    assert seed["window_source"] is None


def test_seed_and_middleware_payload_share_the_wire_key_set():
    """Both producers go through the one builder, asserted on key sets (design §Testing)."""
    seed = derive_context_usage([_ai(usage=USAGE, model="anthropic/claude-sonnet-4.6")])
    built = context_usage_payload(
        model="m", input_tokens=1, output_tokens=1, cached_tokens=0, window_tokens=None, window_source=None
    )

    assert set(seed) == set(built)
