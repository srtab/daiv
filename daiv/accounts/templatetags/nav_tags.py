from __future__ import annotations

from django import template

register = template.Library()

ACTIVE_CLASSES = "bg-white/[0.06] text-white"


@register.simple_tag(takes_context=True)
def nav_active(context, section_key: str) -> str:
    """Return CSS classes when the sidebar item for ``section_key`` is the active section.

    The active section is computed once per request by ``accounts.context_processors.nav``
    and exposed as ``nav_active_section`` in the template context.
    """
    if context.get("nav_active_section") == section_key:
        return ACTIVE_CLASSES
    return ""
