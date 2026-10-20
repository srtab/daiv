"""Telegram's formatter: one ``sendMessage`` per notification.

``parse_mode=HTML``, never MarkdownV2: MarkdownV2 requires escaping roughly 18 characters and
these messages interpolate repo names, branch names and error text — one unescaped ``-`` or
``.`` is a 400. HTML mode needs only ``<``, ``>``, ``&``.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from notifications.channels.renderers.base import TONE_EMOJI, link_of, summary_of, tone_of
from notifications.channels.renderers.events import event_rows

if TYPE_CHECKING:
    from notifications.models import Notification

TG_MAX_CHARS = 4096
VIEW_LABEL = "View in DAIV"

_ELLIPSIS = "…"
# Notification.subject is a 255-char CharField; the budget is a little above that so a normal
# subject is never cut, while a pathological one still cannot crowd out the rest of the message.
_SUBJECT_BUDGET = 300
# Short rows read free-form ``Notification.context`` JSON (``repo_id``, ``trigger_owner``), so
# each is capped too: none may be left unbounded.
_ROW_BUDGET = 200


def truncate_escaped(escaped: str, limit: int) -> str:
    """Cut already-escaped text to ``limit`` characters without splitting an ``&…;`` entity.

    Callers cut the *escaped variable region* to the budget the fixed skeleton leaves, before
    wrapping it in tags — truncating an assembled message can halve a tag, and Telegram answers
    unbalanced HTML with a 400, which the channel files as a permanent failure.
    """
    if limit <= 0:
        return ""
    if len(escaped) <= limit:
        return escaped
    cut = limit - len(_ELLIPSIS)
    if cut <= 0:
        return _ELLIPSIS[:limit]
    head = escaped[:cut]
    amp = head.rfind("&")
    if amp != -1 and ";" not in head[amp:]:
        head = head[:amp]
    return head + _ELLIPSIS


def esc(value: object) -> str:
    """HTML-escape any interpolated value. ``quote=False`` — these never land in an attribute."""
    return html.escape("" if value is None else str(value), quote=False)


def _keyboard(notification: Notification) -> dict:
    """The "View in DAIV" button; ``{}`` without a link, as Telegram rejects a URL button with an empty url."""
    link = link_of(notification)
    if not link:
        return {}
    return {"inline_keyboard": [[{"text": VIEW_LABEL, "url": link}]]}


def render(notification: Notification) -> tuple[str, dict] | None:
    """``(text, reply_markup)`` for ``sendMessage``, or ``None`` when the event has no rows.

    Telegram answers an over-length message with a 400 the channel files as permanent, losing the
    message, so every region is capped: the subject and each short row to a fixed budget, then the
    wide rows and the summary — the extra area — each to whatever of ``TG_MAX_CHARS`` is still left.
    An extra row that no longer fits at all is dropped.
    """
    rows = event_rows(notification)
    if rows is None:
        return None
    ctx = notification.context
    subject = truncate_escaped(esc(notification.subject), _SUBJECT_BUDGET)
    lines = [f"{TONE_EMOJI[tone_of(ctx)]} <b>{subject}</b>", ""]
    lines += [
        f"<b>{esc(row.label)}:</b> {truncate_escaped(esc(row.value), _ROW_BUDGET)}" for row in rows if not row.wide
    ]
    text = "\n".join(lines)

    extra = [(row.label, row.value) for row in rows if row.wide]
    if summary := summary_of(ctx):
        extra.append(("Summary", summary))
    for label, value in extra:
        prefix = f"{text}\n<b>{esc(label)}:</b> "
        if len(prefix) >= TG_MAX_CHARS:
            break
        text = prefix + truncate_escaped(esc(value), TG_MAX_CHARS - len(prefix))
    return text, _keyboard(notification)
