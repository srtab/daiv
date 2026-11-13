"""Discord's formatter: one embed per notification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.renderers.base import Row, link_of, list_lines, summary_of, tone_of
from notifications.channels.renderers.events import event_rows

if TYPE_CHECKING:
    from notifications.models import Notification

COLOR_SUCCESS = 0x22C55E
COLOR_FAILURE = 0xEF4444
COLOR_PARTIAL = 0xEAB308
TONE_COLOR = {"success": COLOR_SUCCESS, "failure": COLOR_FAILURE, "warning": COLOR_PARTIAL}

DISCORD_MAX_CONTENT_CHARS = 2000
DISCORD_MAX_EMBED_CHARS = 6000
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
    if (value := list_lines(items, overflow, mono=mono)) is None:
        return None
    return _field(Row(title, value, wide=True))


def _fit_fields(fields: list[dict], budget: int) -> list[dict]:
    """Retain whole fields where possible, truncating the last value to the aggregate budget."""
    fitted = []
    for field in fields:
        available = budget - len(field["name"])
        if available < 1:
            break
        field = {**field, "value": _truncate(field["value"], available)}
        fitted.append(field)
        budget -= len(field["name"]) + len(field["value"])
    return fitted


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
    fields = [*map(_field, rows), *(field for field in extra if field is not None)][:DISCORD_MAX_FIELDS]
    embed = {
        "color": TONE_COLOR[tone_of(ctx)],
        "title": _truncate(notification.subject, DISCORD_MAX_TITLE_CHARS),
        "footer": {"text": "DAIV"},
        "timestamp": notification.created.isoformat(),
    }
    if link := link_of(notification):
        embed["url"] = link
    description = _truncate(summary_of(ctx), DISCORD_MAX_DESCRIPTION_CHARS)
    if description:
        embed["description"] = description
    used = len(embed["title"]) + len(description) + len(embed["footer"]["text"])
    embed["fields"] = _fit_fields(fields, DISCORD_MAX_EMBED_CHARS - used)
    return embed
