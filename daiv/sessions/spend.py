from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from automation.agent.usage_tracking import collapse_repeated_model_name

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sessions.models import Run

logger = logging.getLogger("daiv.sessions")

_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def build_session_spend(runs: Iterable[Run]) -> dict[str, Any]:
    """Settled spend for a session, aggregated over its ``Run`` rows.

    Takes the rows rather than the session so a caller that already fetched them
    (``SessionDetailView``) doesn't query twice. Every run counts toward ``turns`` regardless
    of status — a cancelled run consumed what it recorded. Rows with nothing recorded add zero
    to the sums and mark the totals as floors, as does any model that ever priced to None; the
    total keeps every priced per-run cost.

    ``cost_usd`` is None when there were tokens to price and none of them priced — distinct
    from a genuine zero, which is what a session with no runs has. Both used to render
    "$0.00+", giving the reader no way to tell "we know nothing" from "this was nearly free".
    """
    totals = dict.fromkeys(_TOKEN_KEYS, 0)
    turns = 0
    unrecorded_runs = 0
    unpriced: set[str] = set()
    any_priced = False
    merged: dict[str, dict[str, Any]] = {}
    total_cost = Decimal("0")

    for run in runs:
        turns += 1
        if run.total_tokens is None and not run.usage_by_model:
            unrecorded_runs += 1
            continue
        for key in _TOKEN_KEYS:
            totals[key] += getattr(run, key) or 0
        for raw_name, usage in (run.usage_by_model or {}).items():
            # Rows written before this fix carry a duplicated name.
            model_name = collapse_repeated_model_name(raw_name)
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
                # A present-but-unparseable cost is a write-path bug, not a missing price; log it
                # so the row's "—" stays traceable to the corrupt row.
                logger.warning("Unparseable cost_usd %r for model %s on run %s", raw_cost, model_name, run.pk)
                unpriced.add(model_name)
                continue
            entry["cost"] += cost
            total_cost += cost
            any_priced = True

    by_model = [
        {
            "model": model_name,
            **{key: entry[key] for key in _TOKEN_KEYS},
            "cost_usd": None if model_name in unpriced else str(entry["cost"]),
        }
        for model_name, entry in sorted(merged.items(), key=lambda item: -item[1]["total_tokens"])
    ]
    # Keyed on the tokens, not on ``unpriced``: a run recording totals but no ``usage_by_model``
    # adds no name to that set, and would otherwise report the "$0.00" this exists to avoid.
    cost_usd = None if totals["total_tokens"] and not any_priced else str(total_cost)
    return {
        "turns": turns,
        **totals,
        "cost_usd": cost_usd,
        # A floor qualifies a total; with no total there is nothing to mark.
        "cost_is_floor": cost_usd is not None and bool(unpriced or unrecorded_runs),
        "unrecorded_runs": unrecorded_runs,
        "by_model": by_model,
    }
