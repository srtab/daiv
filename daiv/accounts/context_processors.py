from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError
from django.utils.functional import SimpleLazyObject

logger = logging.getLogger("daiv.accounts")

SECTION_URL_NAMES: dict[str, set[str]] = {
    "dashboard": {"dashboard"},
    "runs": {"agent_run_new"},
    "activity": {"activity_list", "activity_detail", "activity_stream", "activity_download_md"},
    "schedules": {
        "schedule_list",
        "schedule_create",
        "schedule_update",
        "schedule_delete",
        "schedule_toggle",
        "schedule_run_now",
    },
    "channels": {"user_channels"},
    "api_keys": {"api_keys", "api_key_create", "api_key_revoke"},
    "users": {"user_list", "user_create", "user_update", "user_delete"},
    "configuration": {"site_configuration"},
}


def _resolve_active_section(request) -> str:
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    url_name = match.url_name or ""
    for section_key, names in SECTION_URL_NAMES.items():
        if url_name in names:
            return section_key
    return ""


def running_jobs_count(request, user) -> int:
    """Return the user's running-jobs count, memoized per-request.

    The first caller hits the database; subsequent callers on the same request reuse the
    value via ``request._daiv_running_jobs``. Falls back to 0 and logs on ``DatabaseError``
    so a transient DB failure degrades the nav badge rather than breaking page rendering.
    """
    cached = getattr(request, "_daiv_running_jobs", None)
    if cached is not None:
        return cached

    from activity.models import Activity, ActivityStatus  # local import to avoid circulars

    try:
        running = Activity.objects.by_owner(user).filter(status=ActivityStatus.RUNNING).count()
    except DatabaseError:
        logger.exception("Failed to compute nav_running_jobs for user %s", user.pk)
        running = 0
    request._daiv_running_jobs = running
    return running


def nav(request) -> dict[str, Any]:
    """Supply ``nav_running_jobs`` and ``nav_active_section`` to every authenticated request.

    ``nav_running_jobs`` is wrapped in ``SimpleLazyObject`` so the DB query runs only if the
    template actually references it — non-HTML responses (redirects, HTMX fragments, SSE)
    skip the query entirely.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    return {
        "nav_running_jobs": SimpleLazyObject(lambda: running_jobs_count(request, user)),
        "nav_active_section": _resolve_active_section(request),
    }
