"""Rate-limit helper for UI-initiated runs.

Shares the ``jobs_throttle_rate`` budget used by the API endpoint so that a
single user cannot exceed the configured hourly limit across API+UI combined.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from django.core.cache import cache

from core.site_settings import site_settings

if TYPE_CHECKING:
    from accounts.models import User

_RATE_RE = re.compile(r"^(\d+)/(second|minute|hour|day)$")
_WINDOW_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _parse_rate(rate: str) -> tuple[int, int] | None:
    """Parse ``"20/hour"`` into ``(count, window_seconds)``. Returns ``None`` for empty/invalid."""
    if not rate:
        return None
    m = _RATE_RE.match(rate.strip())
    if not m:
        return None
    return int(m.group(1)), _WINDOW_SECONDS[m.group(2)]


def check_jobs_throttle(user: User) -> bool:
    """Return ``True`` if the user may submit another run, ``False`` if throttled.

    Empty/invalid rate strings are permissive — matches the existing API
    behaviour where a misconfigured rate does not lock everyone out.
    """
    parsed = _parse_rate(site_settings.jobs_throttle_rate)
    if parsed is None:
        return True

    limit, window = parsed
    now = int(time.time())
    bucket = now // window
    key = f"jobs_throttle:{user.pk}:{bucket}"
    if cache.add(key, 1, timeout=window):
        return limit >= 1
    try:
        count = cache.incr(key)
    except ValueError:
        cache.add(key, 1, timeout=window)
        return limit >= 1
    return count <= limit
