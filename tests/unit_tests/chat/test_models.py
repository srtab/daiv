from decimal import Decimal

from django.db import IntegrityError

import pytest
from activity.models import Activity, TriggerType

from automation.agent.usage_tracking import UsageSummary
from chat.models import ChatThread


def _summary(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd="0.000010", by_model=None) -> UsageSummary:
    return UsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        by_model=by_model
        if by_model is not None
        else {
            "m": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost_usd,
            }
        },
    )


def _make_thread() -> ChatThread:
    """Build a ChatThread *not* persisted to the DB — apply_usage_delta is in-memory."""
    return ChatThread(thread_id="t-test", repo_id="a/b", ref="main")


@pytest.mark.django_db
def test_chat_thread_thread_id_is_unique_primary_key(member_user):
    ChatThread.objects.create(thread_id="t-1", user=member_user, repo_id="a/b", ref="main")
    with pytest.raises(IntegrityError):
        ChatThread.objects.create(thread_id="t-1", user=member_user, repo_id="a/b", ref="main")


@pytest.mark.django_db(transaction=True)
async def test_aget_or_create_from_activity_is_idempotent(member_user):
    activity = await Activity.objects.acreate(
        trigger_type=TriggerType.UI_JOB,
        repo_id="a/b",
        ref="main",
        prompt="first message",
        thread_id="t-42",
        user=member_user,
    )
    thread_a, created_a = await ChatThread.aget_or_create_from_activity(member_user, activity)
    thread_b, created_b = await ChatThread.aget_or_create_from_activity(member_user, activity)
    assert created_a is True
    assert created_b is False
    assert thread_a.thread_id == thread_b.thread_id == "t-42"
    assert thread_a.repo_id == "a/b"
    assert thread_a.ref == "main"
    assert thread_a.title.startswith("first message")


def test_apply_usage_delta_increments_token_columns():
    t = _make_thread()
    changed = t.apply_usage_delta(_summary(input_tokens=10, output_tokens=5, total_tokens=15), "m", 10)

    assert t.input_tokens == 10
    assert t.output_tokens == 5
    assert t.total_tokens == 15
    assert "input_tokens" in changed and "output_tokens" in changed and "total_tokens" in changed

    t.apply_usage_delta(_summary(input_tokens=4, output_tokens=2, total_tokens=6), "m", 4)
    assert t.input_tokens == 14
    assert t.output_tokens == 7
    assert t.total_tokens == 21


def test_apply_usage_delta_replaces_last_model_and_input_tokens():
    t = _make_thread()
    t.apply_usage_delta(_summary(), "model-a", 100)
    assert t.last_model_name == "model-a"
    assert t.last_input_tokens == 100

    t.apply_usage_delta(_summary(), "model-b", 50)
    assert t.last_model_name == "model-b"
    assert t.last_input_tokens == 50


def test_apply_usage_delta_accumulates_cost_when_priced():
    t = _make_thread()
    t.apply_usage_delta(_summary(cost_usd="0.10"), "m", 1)
    t.apply_usage_delta(_summary(cost_usd="0.05"), "m", 1)
    assert t.cost_priced is True
    assert t.cost_usd == Decimal("0.15")


def test_apply_usage_delta_unpriced_makes_cost_sticky_null():
    t = _make_thread()
    t.apply_usage_delta(_summary(cost_usd="0.10"), "m", 1)
    t.apply_usage_delta(_summary(cost_usd=None), "m", 1)  # unpriced delta
    assert t.cost_priced is False
    assert t.cost_usd is None

    # Subsequent priced deltas do NOT resurrect cost.
    t.apply_usage_delta(_summary(cost_usd="0.05"), "m", 1)
    assert t.cost_priced is False
    assert t.cost_usd is None


def test_apply_usage_delta_skips_empty_summary():
    t = _make_thread()
    changed = t.apply_usage_delta(_summary(input_tokens=0, output_tokens=0, total_tokens=0, by_model={}), None, 0)
    assert changed == []
    assert t.input_tokens in (None, 0)
    assert t.last_model_name in (None, "")


def test_apply_usage_delta_deep_merges_usage_by_model():
    t = _make_thread()
    t.apply_usage_delta(
        _summary(by_model={"m": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": "0.001"}}),
        "m",
        10,
    )
    t.apply_usage_delta(
        _summary(by_model={"m": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10, "cost_usd": "0.002"}}),
        "m",
        7,
    )
    entry = t.usage_by_model["m"]
    assert entry["input_tokens"] == 17
    assert entry["output_tokens"] == 8
    assert entry["total_tokens"] == 25
    assert Decimal(entry["cost_usd"]) == Decimal("0.003")


def test_apply_usage_delta_malformed_cost_degrades_to_unpriced(caplog):
    t = _make_thread()
    t.apply_usage_delta(_summary(cost_usd="0.10"), "m", 1)
    assert t.cost_usd == Decimal("0.10")

    with caplog.at_level("ERROR", logger="daiv.chat"):
        changed = t.apply_usage_delta(_summary(cost_usd="not-a-number"), "m", 1)

    assert t.cost_priced is False
    assert t.cost_usd is None
    assert "cost_priced" in changed
    assert "cost_usd" in changed
    assert any("Invalid cost_usd" in r.message for r in caplog.records)


def test_apply_usage_delta_malformed_cost_typeerror_branch(caplog):
    """Decimal(object()) raises TypeError — covered by the same except clause."""
    t = _make_thread()
    summary = UsageSummary(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_usd=object(),  # not a number, not a string
        by_model={},
    )
    with caplog.at_level("ERROR", logger="daiv.chat"):
        t.apply_usage_delta(summary, "m", 1)
    assert t.cost_priced is False
    assert t.cost_usd is None


def test_apply_usage_delta_per_model_malformed_cost_logs_and_nulls(caplog):
    t = _make_thread()
    t.usage_by_model = {"m": {"cost_usd": "0.10"}}
    by_model = {"m": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "cost_usd": "garbage"}}
    summary = UsageSummary(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd="0.10", by_model=by_model)

    with caplog.at_level("WARNING", logger="daiv.chat"):
        t.apply_usage_delta(summary, "m", 1)

    assert t.usage_by_model["m"]["cost_usd"] is None
    assert any("Invalid per-model cost" in r.message for r in caplog.records)


def test_apply_usage_delta_skips_usage_by_model_write_when_empty():
    """Non-zero token totals with empty by_model must not touch usage_by_model."""
    t = _make_thread()
    t.usage_by_model = None  # untouched by the call
    summary = UsageSummary(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd="0.001", by_model={})

    changed = t.apply_usage_delta(summary, "m", 10)

    assert "usage_by_model" not in changed
    assert t.usage_by_model is None
    # Token totals still accumulated.
    assert t.input_tokens == 10
    assert "input_tokens" in changed


def test_apply_usage_delta_increments_cache_columns():
    t = _make_thread()
    by_model = {
        "m": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cost_usd": "0.001",
            "input_token_details": {"cache_read": 100, "cache_creation": 5, "ephemeral_5m_input_tokens": 10},
        }
    }
    t.apply_usage_delta(
        UsageSummary(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd="0.001", by_model=by_model), "m", 10
    )
    assert t.cache_read_tokens == 100
    assert t.cache_write_tokens == 15  # 5 + 10
