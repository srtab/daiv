from langchain_core.messages import AIMessage, HumanMessage

from chat.usage import aggregate_messages_usage


def _ai(model_name: str, input_tokens: int, output_tokens: int) -> AIMessage:
    msg = AIMessage(content="x")
    msg.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    msg.response_metadata = {"model_name": model_name}
    return msg


def test_aggregate_empty_messages_returns_zero_summary():
    summary = aggregate_messages_usage([])
    assert summary.total_tokens == 0
    assert summary.input_tokens == 0
    assert summary.output_tokens == 0
    assert summary.by_model == {}


def test_aggregate_skips_human_messages():
    summary = aggregate_messages_usage([HumanMessage(content="hi")])
    assert summary.total_tokens == 0


def test_aggregate_skips_ai_messages_without_usage_metadata():
    summary = aggregate_messages_usage([AIMessage(content="hi")])
    assert summary.total_tokens == 0


def test_aggregate_merges_same_model():
    msgs = [_ai("anthropic/claude-sonnet-4.6", 10, 5), _ai("anthropic/claude-sonnet-4.6", 4, 6)]
    summary = aggregate_messages_usage(msgs)
    assert summary.input_tokens == 14
    assert summary.output_tokens == 11
    assert summary.total_tokens == 25
    assert set(summary.by_model.keys()) == {"anthropic/claude-sonnet-4.6"}


def test_aggregate_separates_distinct_models():
    msgs = [_ai("anthropic/claude-sonnet-4.6", 10, 5), _ai("openai/gpt-4o", 3, 2)]
    summary = aggregate_messages_usage(msgs)
    assert set(summary.by_model.keys()) == {"anthropic/claude-sonnet-4.6", "openai/gpt-4o"}
    assert summary.total_tokens == 20
