"""Telegram message assembly.

``parse_mode=HTML``, never MarkdownV2: MarkdownV2 requires escaping roughly 18 characters and
these messages interpolate repo names, branch names and error text — one unescaped ``-`` or
``.`` is a 400. HTML mode needs only ``<``, ``>``, ``&``.
"""

from __future__ import annotations

import html
from abc import abstractmethod
from typing import TYPE_CHECKING

from notifications.channels.renderers.base import TONE_EMOJI, BaseRenderer

if TYPE_CHECKING:
    from notifications.models import Notification

TG_MAX_CHARS = 4096
VIEW_LABEL = "View in DAIV"

_ELLIPSIS = "…"
# Notification.subject is a 255-char CharField; the budget is a little above that so a normal
# subject is never cut, while a pathological one still cannot crowd out the rest of the message.
_SUBJECT_BUDGET = 300


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


class TelegramRenderer(BaseRenderer):
    """Base for per-event Telegram renderers.

    ``render`` returns ``(html_text, reply_markup)``. ``reply_markup`` is ``{}`` when the
    notification carries no link, because Telegram rejects a URL button with an empty url.
    """

    @abstractmethod
    def render(self, notification: Notification) -> tuple[str, dict]:
        """Return ``(text, reply_markup)`` for ``sendMessage`` with ``parse_mode=HTML``."""

    @staticmethod
    def esc(value: object) -> str:
        """HTML-escape any interpolated value. ``quote=False`` — these never land in an attribute."""
        return html.escape("" if value is None else str(value), quote=False)

    def _keyboard(self, notification: Notification) -> dict:
        link = self._link(notification)
        if not link:
            return {}
        return {"inline_keyboard": [[{"text": VIEW_LABEL, "url": link}]]}

    def _usage_rows(self, ctx: dict, *, cost_label: str = "Cost") -> list[tuple[str, str]]:
        """Usage and cost rows — zero, one, or two entries depending on available context."""
        rows: list[tuple[str, str]] = []
        in_tokens = self._fmt_tokens(ctx.get("input_tokens"))
        out_tokens = self._fmt_tokens(ctx.get("output_tokens"))
        if in_tokens is not None or out_tokens is not None:
            rows.append(("Usage", f"{in_tokens or '—'} in · {out_tokens or '—'} out"))
        if (cost := self._fmt_cost(ctx.get("cost_usd"))) is not None:
            rows.append((cost_label, cost))
        return rows

    def _assemble(
        self, notification: Notification, ctx: dict, rows: list[tuple[str, str]], extra: tuple[str, str] | None = None
    ) -> tuple[str, dict]:
        """Build the shared message shape.

        ``rows`` are ``(label, value)`` pairs, both escaped here; they are short by
        construction. ``extra`` is the one variable-length region — ``(label, already-escaped
        text)`` — and is fitted to whatever the fixed skeleton leaves of the 4096 budget.
        """
        emoji = TONE_EMOJI[self._tone(ctx)]
        subject = truncate_escaped(self.esc(notification.subject), _SUBJECT_BUDGET)
        lines = [f"{emoji} <b>{subject}</b>", ""]
        lines += [f"<b>{self.esc(label)}:</b> {self.esc(value)}" for label, value in rows]
        text = "\n".join(lines)
        if extra is not None:
            label, escaped_value = extra
            prefix = f"{text}\n<b>{self.esc(label)}:</b> "
            text = prefix + truncate_escaped(escaped_value, TG_MAX_CHARS - len(prefix))
        return text, self._keyboard(notification)
