"""Discord's formatter: one embed per notification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.renderers.base import Row, link_of, summary_of, tone_of
from notifications.channels.renderers.events import event_rows

if TYPE_CHECKING:
    from notifications.models import Notification

COLOR_SUCCESS = 0x22C55E
COLOR_FAILURE = 0xEF4444
COLOR_PARTIAL = 0xEAB308
TONE_COLOR = {"success": COLOR_SUCCESS, "failure": COLOR_FAILURE, "warning": COLOR_PARTIAL}

DISCORD_MAX_CONTENT_CHARS = 2000
DISCORD_MAX_FIELDS = 25
DISCORD_MAX_FIELD_NAME_CHARS = 256
DISCORD_MAX_FIELD_VALUE_CHARS = 1024
DISCORD_MAX_TITLE_CHARS = 256
DISCORD_MAX_DESCRIPTION_CHARS = 4096


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _field(row: Row) -> dict:
    return {
        "name": _truncate(row.label, DISCORD_MAX_FIELD_NAME_CHARS),
        "value": _truncate(row.value, DISCORD_MAX_FIELD_VALUE_CHARS),
        "inline": not row.wide,
    }


def _list_field(title: str, items: list[dict] | None, overflow: int, *, mono: bool = True) -> dict | None:
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
    return _field(Row(title, "\n".join(lines), wide=True))


def render(notification: Notification) -> dict | None:
    """Return one Discord embed, or ``None`` when the event has no renderer rows."""
    rows = event_rows(notification)
    if rows is None:
        return None
    ctx = notification.context
    extra = (
        _list_field("Findings", ctx.get("actionable"), ctx.get("actionable_overflow") or 0),
        _list_field("Needs a look", ctx.get("notable_runs"), ctx.get("notable_runs_overflow") or 0, mono=False),
    )
    fields = [*map(_field, rows), *(field for field in extra if field is not None)]
    embed = {
        "color": TONE_COLOR[tone_of(ctx)],
        "title": _truncate(notification.subject, DISCORD_MAX_TITLE_CHARS),
        "url": link_of(notification),
        "fields": fields[:DISCORD_MAX_FIELDS],
        "footer": {"text": "DAIV"},
        "timestamp": notification.created.isoformat(),
    }
    if summary := summary_of(ctx):
        embed["description"] = _truncate(summary, DISCORD_MAX_DESCRIPTION_CHARS)
    return embed
