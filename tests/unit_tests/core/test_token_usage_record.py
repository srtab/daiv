from __future__ import annotations

from decimal import Decimal

from automation.agent.usage_tracking import UsageSummary
from core.models import TokenUsageRecord


class _UsageRecord(TokenUsageRecord):
    """Concrete subclass for testing the mixin in isolation. Never saved."""

    class Meta:
        app_label = "core"
        managed = False  # No DB table; we only exercise in-memory methods.


def _make(**overrides) -> _UsageRecord:
    return _UsageRecord(**overrides)


def test_apply_usage_snapshot_sets_all_fields_when_none():
    rec = _make()
    summary = UsageSummary(
        input_tokens=100, output_tokens=50, total_tokens=150, cost_usd="0.001234", by_model={"m": {"input_tokens": 100}}
    )

    changed = rec.apply_usage_snapshot(summary)

    assert rec.input_tokens == 100
    assert rec.output_tokens == 50
    assert rec.total_tokens == 150
    assert rec.cost_usd == Decimal("0.001234")
    assert rec.usage_by_model == {"m": {"input_tokens": 100}}
    assert set(changed) == {"input_tokens", "output_tokens", "total_tokens", "cost_usd", "usage_by_model"}


def test_apply_usage_snapshot_idempotent_on_populated_row():
    rec = _make(
        input_tokens=10, output_tokens=20, total_tokens=30, cost_usd=Decimal("0.5"), usage_by_model={"existing": {}}
    )
    summary = UsageSummary(input_tokens=999, output_tokens=999, total_tokens=999, cost_usd="9.0", by_model={"new": {}})

    changed = rec.apply_usage_snapshot(summary)

    assert rec.input_tokens == 10
    assert rec.output_tokens == 20
    assert rec.total_tokens == 30
    assert rec.cost_usd == Decimal("0.5")
    assert rec.usage_by_model == {"existing": {}}
    assert changed == []


def test_apply_usage_snapshot_skips_none_summary_fields():
    rec = _make()
    summary = UsageSummary(input_tokens=100, output_tokens=50, total_tokens=150, cost_usd=None, by_model={})

    changed = rec.apply_usage_snapshot(summary)

    assert rec.input_tokens == 100
    assert rec.cost_usd is None
    assert "cost_usd" not in changed


def test_apply_usage_snapshot_logs_and_skips_invalid_decimal(caplog):
    rec = _make()
    summary = UsageSummary(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd="not-a-decimal", by_model={})

    with caplog.at_level("WARNING"):
        changed = rec.apply_usage_snapshot(summary)

    assert rec.cost_usd is None
    assert "cost_usd" not in changed
    assert any("not-a-decimal" in r.message for r in caplog.records)
