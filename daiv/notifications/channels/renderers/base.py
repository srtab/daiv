"""Format-agnostic renderer helpers shared by every chat channel.

Tone is channel-neutral by design: the notifiers stamp ``context["status_tone"]`` and every
channel reads it rather than re-deriving tone from ``status``. This module owns the tone *key*
and the tone → emoji map; per-channel styling (Rocket Chat's hex colours) stays with the channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.utils import build_absolute_url

if TYPE_CHECKING:
    from notifications.models import Notification

TONE_EMOJI = {"success": "✅", "failure": "❌", "warning": "⚠️"}

REPO_BREAKDOWN_LIMIT = 8


@dataclass(frozen=True)
class Row:
    """One labelled value of an event's channel-neutral content.

    ``wide`` marks a value whose length is not bounded by construction (a list, free text, a URL):
    Rocket Chat gives it a full-width field, Telegram fits it into the budget its other rows leave.
    """

    label: str
    value: str
    wide: bool = False


def compose_plain_text(notification: Notification) -> str:
    """The unstyled subject/body/link form every channel falls back to without a renderer."""
    parts = [notification.subject, "", notification.body]
    if link := link_of(notification):
        parts.extend(["", link])
    return "\n".join(parts)


def fmt_tokens(n: int | None) -> str | None:
    if n is None:
        return None
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def fmt_cost(usd: float | None) -> str | None:
    if usd is None:
        return None
    return f"${usd:.2f}"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def usage_rows(ctx: dict, *, cost_label: str = "Cost") -> list[Row]:
    """Usage and cost rows — zero, one, or two, depending on what the context carries."""
    rows: list[Row] = []
    in_tokens = fmt_tokens(ctx.get("input_tokens"))
    out_tokens = fmt_tokens(ctx.get("output_tokens"))
    if in_tokens is not None or out_tokens is not None:
        rows.append(Row("Usage", f"{in_tokens or '—'} in · {out_tokens or '—'} out"))
    if (cost := fmt_cost(ctx.get("cost_usd"))) is not None:
        rows.append(Row(cost_label, cost))
    return rows


def breakdown_value(ctx: dict) -> str:
    """The batch rollup's per-status counts; the question count only when some run is waiting."""
    breakdown = (
        f"found {ctx.get('found_count', 0)} · needs {ctx.get('needs_attention_count', 0)}"
        f" · failed {ctx.get('failed_count', 0)}"
    )
    if needs_input := ctx.get("needs_input_count", 0):
        breakdown += f" · input {needs_input}"
    return breakdown


def repo_list(repo_ids: list[str]) -> str:
    """The batch rollup's repository breakdown, capped at ``REPO_BREAKDOWN_LIMIT``."""
    if not repo_ids:
        return ""
    head = repo_ids[:REPO_BREAKDOWN_LIMIT]
    overflow = len(repo_ids) - len(head)
    parts = list(head)
    if overflow > 0:
        parts.append(f"… and {overflow} more")
    return " · ".join(parts)


def link_of(notification: Notification) -> str:
    return build_absolute_url(notification.link_url) if notification.link_url else ""


def tone_of(ctx: dict) -> str:
    """The ``{success, warning, failure}`` key for this notification.

    Reads the ``status_tone`` the notifiers stamp, falling back to the legacy
    ``is_successful`` for contexts written before that key existed. No channel should
    re-derive tone from ``status``.
    """
    tone = ctx.get("status_tone")
    if tone in TONE_EMOJI:
        return tone
    return "success" if ctx.get("is_successful") else "failure"


def summary_of(ctx: dict) -> str:
    """The classifier's summary, stripped; empty when the context carries none."""
    return (ctx.get("summary") or "").strip()
