from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError
from django.utils.functional import SimpleLazyObject

logger = logging.getLogger("daiv.accounts")

# Section key for the admin-only global MCP servers page. Named because view code
# also references it (via ``request.nav_section_override``) to pin the section on the
# edit/delete URLs shared with the personal page — a bare literal would drift silently.
NAV_SECTION_MCP_GLOBAL = "mcp_servers_global"

SECTION_URL_NAMES: dict[str, set[str]] = {
    "dashboard": {"dashboard"},
    "sessions": {
        "session_list",
        "session_new",
        "session_new_chat",
        "session_detail",
        "session_stream",
        "session_run_download_md",
        # ``runs`` namespace (include(..., namespace="runs")) — match.view_name is prefixed,
        # so the bare name would never highlight the sidebar on the "Start a run" page.
        "runs:agent_run_new",
    },
    "schedules": {
        "schedule_list",
        "schedule_create",
        "schedule_update",
        "schedule_delete",
        "schedule_toggle",
        "schedule_run_now",
    },
    "schedule_templates": {
        "schedule_template_list",
        "schedule_template_create",
        "schedule_template_update",
        "schedule_template_delete",
    },
    "channels": {"user_channels"},
    "api_keys": {"api_keys", "api_key_create", "api_key_revoke"},
    "users": {"user_list", "user_create", "user_update", "user_delete"},
    "configuration": {"site_configuration", "site_configuration_index"},
    "skills": {"skills:list", "skills:upload", "skills:detail", "skills:delete", "skills:download"},
    "sandbox_envs": {
        "sandbox_envs:list",
        "sandbox_envs:create",
        "sandbox_envs:edit",
        "sandbox_envs:delete",
        "sandbox_envs:set_default",
    },
    "memory": {"memory:list", "memory:detail", "memory:consolidate"},
    # Only page-rendering routes need a section for sidebar highlighting. The endpoints that only
    # return JSON (``test``) or redirect (``toggle``, ``refresh_tools``) never render a sidebar, so
    # they are omitted; ``delete`` stays because its GET renders a confirmation page.
    "mcp_servers": {"mcp_servers:list", "mcp_servers:create", "mcp_servers:edit", "mcp_servers:delete"},
    NAV_SECTION_MCP_GLOBAL: {"mcp_servers:global_list", "mcp_servers:global_create"},
}


def _resolve_active_section(request) -> str:
    # A view may pin the section explicitly — needed where one URL serves rows of
    # several sections (e.g. mcp_servers:edit renders global AND personal rows).
    override = getattr(request, "nav_section_override", None)
    if override:
        return override
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    view_name = match.view_name or ""
    for section_key, names in SECTION_URL_NAMES.items():
        if view_name in names:
            return section_key
    return ""


def query_running_jobs(user) -> int:
    """Count the runs ``user`` can see that are currently RUNNING.

    The nav badge's single source of truth: the first page render reads it through
    ``nav`` below, and every subsequent update comes from the SSE endpoint
    (``accounts.api.views``) recomputing it on a ``core.ui_events`` poke. Falls back to 0
    and logs on ``DatabaseError`` so a transient DB failure degrades the badge rather
    than breaking the page (or dropping the stream).

    SYNC ONLY — ``Run.objects.visible_to`` resolves the caller's platform identity with a
    DB read at query-build time; async callers must wrap this in ``sync_to_async``.
    """
    from sessions.models import Run, RunStatus  # local import to avoid circulars

    try:
        return Run.objects.visible_to(user).filter(status=RunStatus.RUNNING).count()
    except DatabaseError:
        logger.exception("Failed to compute nav_running_jobs for user %s", user.pk)
        return 0


def running_jobs_count(request, user) -> int:
    """``query_running_jobs`` memoized per-request via ``request._daiv_running_jobs``."""
    cached = getattr(request, "_daiv_running_jobs", None)
    if cached is not None:
        return cached
    running = query_running_jobs(user)
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

    from codebase.conf import settings as codebase_settings

    return {
        "nav_running_jobs": SimpleLazyObject(lambda: running_jobs_count(request, user)),
        "nav_active_section": _resolve_active_section(request),
        "git_platform": codebase_settings.CLIENT.value,
    }
