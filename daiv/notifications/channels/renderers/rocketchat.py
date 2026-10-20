"""Rocket Chat's formatter: one ``chat.postMessage`` attachment per notification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.renderers.base import TONE_EMOJI, link_of, summary_of, tone_of
from notifications.channels.renderers.events import event_rows

if TYPE_CHECKING:
    from notifications.channels.renderers.base import Row
    from notifications.models import Notification

COLOR_SUCCESS = "#22c55e"
COLOR_FAILURE = "#ef4444"
COLOR_PARTIAL = "#eab308"

TONE_COLOR = {"success": COLOR_SUCCESS, "failure": COLOR_FAILURE, "warning": COLOR_PARTIAL}

FOOTER = "DAIV"


def _field(row: Row) -> dict:
    return {"title": row.label, "value": row.value, "short": not row.wide}


def _list_field(title: str, items: list[dict] | None, overflow: int, *, mono: bool = True) -> dict | None:
    """One long field listing ``{kind, label, ref}`` rows; ``None`` when there are none.

    ``mono`` backticks ``ref`` — right for a file path, wrong for the prose a batch row carries.
    """
    if not items:
        return None
    lines = []
    for item in items:
        prefix = f"[{item['kind']}] " if item.get("kind") else ""
        ref = item.get("ref")
        tail = (f" — `{ref}`" if mono else f" — {ref}") if ref else ""
        lines.append(f"• {prefix}{item.get('label', '')}{tail}")
    if overflow > 0:
        lines.append(f"… and {overflow} more")
    return {"title": title, "value": "\n".join(lines), "short": False}


def render(notification: Notification) -> tuple[str, list[dict]] | None:
    """``(text, attachments)`` for ``chat.postMessage``, or ``None`` when the event has no rows.

    The findings and batch lists are Rocket Chat's own extension, appended after the event's rows.
    """
    rows = event_rows(notification)
    if rows is None:
        return None
    ctx = notification.context
    extra = (
        _list_field("Findings", ctx.get("actionable"), ctx.get("actionable_overflow") or 0),
        _list_field("Needs a look", ctx.get("notable_runs"), ctx.get("notable_runs_overflow") or 0, mono=False),
    )
    tone = tone_of(ctx)
    attachment = {
        "color": TONE_COLOR[tone],
        "title": notification.subject,
        "title_link": link_of(notification),
        "fields": [*map(_field, rows), *(field for field in extra if field is not None)],
        "footer": FOOTER,
        "ts": int(notification.created.timestamp()),
    }
    if summary := summary_of(ctx):
        attachment["text"] = summary
    return f"{TONE_EMOJI[tone]} {notification.subject}", [attachment]
