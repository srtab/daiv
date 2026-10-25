from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError
from django.utils.functional import SimpleLazyObject

from sessions.models import Run, RunStatus

logger = logging.getLogger("daiv.dashboard")

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
    "artifacts": {"artifact_list", "session_artifact_detail", "session_artifact_raw"},
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
    "notifications": {"notifications:list"},
    # The account menu's pages; the mfa_* names are passkey (WebAuthn) pages under /accounts/mfa/.
    "account": {
        "user_channels",
        "api_keys",
        "api_key_create",
        "api_key_revoke",
        "mfa_index",
        "mfa_list_webauthn",
        "mfa_add_webauthn",
        "mfa_edit_webauthn",
        "mfa_remove_webauthn",
        "mfa_reauthenticate_webauthn",
    },
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
    "mcp_servers": {"mcp_connectors:list", "mcp_connectors:create", "mcp_connectors:edit", "mcp_connectors:delete"},
    "mcp_servers_global": {"mcp_connectors:global_list", "mcp_connectors:global_create"},
    "plugins": {"plugins:list", "plugins:add", "plugins:detail", "plugins:delete"},
    "plugins_global": {"plugins:global_list"},
}


def _resolve_active_section(request) -> str:
    # A view may borrow another URL's section — needed where one URL serves rows of
    # several sections (e.g. mcp_connectors:edit renders global AND personal rows).
    match = getattr(request, "resolver_match", None)
    view_name = getattr(request, "nav_view_name", None) or (match.view_name if match else "")
    for section_key, names in SECTION_URL_NAMES.items():
        if view_name in names:
            return section_key
    return ""


def count_running(visible) -> int:
    """RUNNING rows in a ``Run.objects.visible_to(user)`` queryset.

    ``values("pk")`` keeps the DISTINCT of the visibility join off every Run column.
    """
    return visible.filter(status=RunStatus.RUNNING).values("pk").count()


def query_running_jobs(user) -> int:
    """Count the runs ``user`` can see that are currently RUNNING.

    The nav badge's single source of truth: the first page render reads it through
    ``nav`` below, and every subsequent update comes from the SSE endpoint
    (``dashboard.api.views``) recomputing it on a ``core.ui_events`` poke. Raises on
    ``DatabaseError`` so each caller picks its own degradation — a page render shows 0,
    but a live stream must keep the browser's last value rather than push a zero it
    cannot distinguish from a real one.

    SYNC ONLY — ``visible_to`` resolves platform identity with a DB read at query-build time.
    """
    return count_running(Run.objects.visible_to(user))


def running_jobs_count(request, user) -> int:
    """``query_running_jobs`` memoized per-request via ``request._daiv_running_jobs``.

    Degrades to 0 on ``DatabaseError`` so a transient DB failure costs the badge rather
    than the whole page render.
    """
    cached = getattr(request, "_daiv_running_jobs", None)
    if cached is not None:
        return cached
    try:
        running = query_running_jobs(user)
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
