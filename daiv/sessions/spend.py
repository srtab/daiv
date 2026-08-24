from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sessions.models import Session

_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def build_session_spend(session: Session) -> dict[str, Any]:
    """Settled spend for a session, aggregated from its ``Run`` rows (context-meter design §4).

    Every run counts toward ``turns`` regardless of status — a cancelled run consumed what it
    recorded. Rows with nothing recorded add zero to the sums and mark the totals as floors,
    as does any model that ever priced to None; the total keeps every priced per-run cost.
    """
    totals = dict.fromkeys(_TOKEN_KEYS, 0)
    turns = 0
    unrecorded_runs = 0
    unpriced: set[str] = set()
    merged: dict[str, dict[str, Any]] = {}
    total_cost = Decimal("0")

    for run in session.runs.all():
        turns += 1
        if run.total_tokens is None and not run.usage_by_model:
            unrecorded_runs += 1
            continue
        for key in _TOKEN_KEYS:
            totals[key] += getattr(run, key) or 0
        for model_name, usage in (run.usage_by_model or {}).items():
            entry = merged.setdefault(model_name, dict.fromkeys(_TOKEN_KEYS, 0) | {"cost": Decimal("0")})
            for key in _TOKEN_KEYS:
                entry[key] += usage.get(key) or 0
            raw_cost = usage.get("cost_usd")
            if raw_cost is None:
                unpriced.add(model_name)
                continue
            try:
                cost = Decimal(raw_cost)
            except InvalidOperation, ValueError, TypeError:
                unpriced.add(model_name)
                continue
            entry["cost"] += cost
            total_cost += cost

    by_model = [
        {
            "model": model_name,
            **{key: entry[key] for key in _TOKEN_KEYS},
            "cost_usd": None if model_name in unpriced else str(entry["cost"]),
        }
        for model_name, entry in sorted(merged.items(), key=lambda item: -item[1]["total_tokens"])
    ]
    return {
        "turns": turns,
        **totals,
        "cost_usd": str(total_cost),
        "cost_is_floor": bool(unpriced or unrecorded_runs),
        "unpriced_models": sorted(unpriced),
        "unrecorded_runs": unrecorded_runs,
        "by_model": by_model,
    }
