from __future__ import annotations

from ninja import Schema


class SpendRowSchema(Schema):
    """One spend record, as the CSV export serves it; empty values are null."""

    occurred_at: str
    kind: str
    task: str | None
    trigger_type: str | None
    person: str
    person_kind: str
    repo_id: str | None
    session_id: str | None
    run_id: str | None
    schedule_id: int | None
    model: str
    provider: str | None
    provider_name: str | None
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_usd: str
    reported_cost_usd: str | None
    unpriced_tokens: int
    cache_reported: bool
    cache_saving_usd: str
    cache_unpriced_tokens: int
    cost_source: str


class SpendPageSchema(Schema):
    items: list[SpendRowSchema]
    next_cursor: str | None
