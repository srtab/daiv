from __future__ import annotations

from django import template
from django.utils.safestring import mark_safe

import markdown
import nh3

register = template.Library()

_md = markdown.Markdown(extensions=["fenced_code", "tables", "nl2br"])


@register.filter(name="render_markdown")
def render_markdown(value: str) -> str:
    """Convert a markdown string to HTML."""
    if not value:
        return ""
    _md.reset()
    html = _md.convert(value)
    return mark_safe(nh3.clean(html))  # noqa: S308 -- nh3.clean() sanitizes the HTML
