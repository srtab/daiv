from __future__ import annotations

from typing import TYPE_CHECKING

from django import template
from django.utils import timezone

if TYPE_CHECKING:
    from datetime import datetime

register = template.Library()


@register.filter
def short_timesince(value: datetime) -> str:
    """Compact relative time — '3m', '23h', '4d', '2w', '3mo', '1y'."""
    if not value:
        return ""
    now = timezone.now()
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w"
    months = days // 30
    if months < 12:
        return f"{months}mo"
    return f"{days // 365}y"
