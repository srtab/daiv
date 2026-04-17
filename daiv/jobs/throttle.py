"""Rate-limit helper for UI-initiated runs.

Uses the ``jobs_throttle_rate`` setting string but keeps its own per-user
bucket — independent of the ninja throttle used by the API endpoint. Both
surfaces honour the same configured rate, so a user can accumulate up to 2×
the limit by alternating channels. Unifying the counter is deferred until we
see the cross-channel abuse in practice.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from django.core.cache import cache

from core.site_settings import site_settings

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.jobs")

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

    Fails open on empty or malformed rate strings so a misconfigured setting
    never locks every user out — but logs a warning so operators can fix it.
    """
    raw_rate = site_settings.jobs_throttle_rate
    parsed = _parse_rate(raw_rate)
    if parsed is None:
        if raw_rate:
            logger.warning("Invalid jobs_throttle_rate %r; UI throttling disabled", raw_rate)
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
        # The key vanished between ``add`` and ``incr`` (eviction or race).
        # Resetting the counter could let a user exceed the limit under contention,
        # so log it and treat the new write as the first hit of a fresh window.
        logger.warning("jobs_throttle key %s vanished between add and incr", key)
        cache.add(key, 1, timeout=window)
        return limit >= 1
    return count <= limit
