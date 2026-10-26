from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError
from django.db.models import Count, Max
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


def running_snapshot(visible) -> dict[str, Any]:
    """The RUNNING rows of a ``Run.objects.visible_to(user)`` queryset, under the nav stream's keys.

    ``running_runs`` is the badge. ``latest_running_start`` moves whenever another run starts, even when one
    finishing at the same moment leaves the count unchanged, which is how the session page hears that a run
    started on its session after it rendered. ``values()`` keeps the DISTINCT of the visibility join off every
    other Run column.
    """
    stats = (
        visible
        .filter(status=RunStatus.RUNNING)
        .values("pk", "started_at")
        .aggregate(running_runs=Count("pk"), latest=Max("started_at"))
    )
    latest = stats["latest"]
    return {"running_runs": stats["running_runs"], "latest_running_start": latest.isoformat() if latest else ""}


def query_running_snapshot(user) -> dict[str, Any]:
    """``running_snapshot`` of the runs ``user`` can see.

    The nav's single source of truth: the first page render reads it through ``nav`` below,
    and every subsequent update comes from the SSE endpoint (``dashboard.api.views``)
    recomputing it on a ``core.ui_events`` poke. Raises on ``DatabaseError`` so each caller
    picks its own degradation — a page render shows 0, but a live stream must keep the
    browser's last value rather than push a zero it cannot distinguish from a real one.

    SYNC ONLY — ``visible_to`` resolves platform identity with a DB read at query-build time.
    """
    return running_snapshot(Run.objects.visible_to(user))


def running_state(request, user) -> dict[str, Any]:
    """``query_running_snapshot`` memoized per-request via ``request._daiv_running_state``.

    Degrades to nothing running on ``DatabaseError`` so a transient DB failure costs the
    badge rather than the whole page render.
    """
    cached = getattr(request, "_daiv_running_state", None)
    if cached is not None:
        return cached
    try:
        state = query_running_snapshot(user)
    except DatabaseError:
        logger.exception("Failed to compute nav_running_jobs for user %s", user.pk)
        state = {"running_runs": 0, "latest_running_start": ""}
    request._daiv_running_state = state
    return state


def running_jobs_count(request, user) -> int:
    return running_state(request, user)["running_runs"]


def nav(request) -> dict[str, Any]:
    """Supply ``nav_running_jobs``, ``nav_latest_running_start`` and ``nav_active_section`` to every
    authenticated request.

    The running values are wrapped in ``SimpleLazyObject`` so the DB query runs only if the
    template actually references them — non-HTML responses (redirects, HTMX fragments, SSE)
    skip the query entirely.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    return {
        "nav_running_jobs": SimpleLazyObject(lambda: running_jobs_count(request, user)),
        "nav_latest_running_start": SimpleLazyObject(lambda: running_state(request, user)["latest_running_start"]),
        "nav_active_section": _resolve_active_section(request),
    }
