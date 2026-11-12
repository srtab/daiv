from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django import template
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext

from sessions.models import RunStatus

if TYPE_CHECKING:
    from datetime import datetime

register = template.Library()

# `sidebar__nav-item--active` draws the 3px brand rail (see input.css); it is part of the
# active state so no inactive item has to carry a hidden copy of the indicator.
ACTIVE_CLASSES = "bg-surface-3 text-text-strong sidebar__nav-item--active"

_WEEK = timedelta(days=7)


@register.simple_tag(takes_context=True)
def nav_active(context, section_key: str) -> str:
    """Return CSS classes when the sidebar item for ``section_key`` is the active section.

    The active section is computed once per request by ``dashboard.context_processors.nav``
    and exposed as ``nav_active_section`` in the template context.
    """
    return nav_active_if(context.get("nav_active_section") == section_key)


@register.simple_tag
def nav_active_if(active: bool) -> str:
    """``nav_active``'s classes for an item whose active state is not one section, like the Settings row."""
    return ACTIVE_CLASSES if active else ""


@register.filter
def compact_time(moment: datetime) -> str:
    """A few characters of relative time for a sidebar row: "5m", "3h", "2d", "in 3h", or the date a week away."""
    delta = moment - timezone.now()
    distance = abs(delta)
    if distance >= _WEEK:
        return date_format(timezone.localtime(moment), "M j")
    if distance >= timedelta(days=1):
        short = gettext("%(count)dd") % {"count": distance.days}
    elif distance >= timedelta(hours=1):
        short = gettext("%(count)dh") % {"count": distance.seconds // 3600}
    elif distance >= timedelta(minutes=1):
        short = gettext("%(count)dm") % {"count": distance.seconds // 60}
    else:
        return gettext("now")
    return gettext("in %(time)s") % {"time": short} if delta > timedelta(0) else short


@register.filter
def run_status_label(status: str) -> str:
    return str(RunStatus(status).label) if status in RunStatus.values else ""
