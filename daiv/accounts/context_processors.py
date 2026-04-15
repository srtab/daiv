from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError

logger = logging.getLogger("daiv.accounts")

SECTION_URL_NAMES: dict[str, set[str]] = {
    "dashboard": {"dashboard"},
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


def nav(request) -> dict[str, Any]:
    """Supply ``nav_running_jobs`` and ``nav_active_section`` to every authenticated request."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    from activity.models import Activity, ActivityStatus  # local import to avoid circulars

    try:
        running = Activity.objects.by_owner(user).filter(status=ActivityStatus.RUNNING).count()
    except DatabaseError:
        logger.exception("Failed to compute nav_running_jobs for user %s", user.pk)
        running = 0

    return {"nav_running_jobs": running, "nav_active_section": _resolve_active_section(request)}
