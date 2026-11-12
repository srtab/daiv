from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError
from django.db.models import Count, Exists, Max, OuterRef
from django.utils.functional import SimpleLazyObject

from sessions.models import Run, RunStatus, Session

from dashboard.sidebar import SidebarLists, sidebar_lists

logger = logging.getLogger("daiv.dashboard")

_MAIN_SECTION_URL_NAMES: dict[str, set[str]] = {
    "dashboard": {"dashboard"},
    "insights": {"insights"},
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
}

_SETTINGS_SECTION_URL_NAMES: dict[str, set[str]] = {
    "schedule_templates": {
        "schedule_template_list",
        "schedule_template_create",
        "schedule_template_update",
        "schedule_template_delete",
    },
    "users": {"user_list", "user_create", "user_update", "user_delete"},
    "configuration": {"site_configuration", "site_configuration_index"},
    "skills": {
        "skills:list",
        "skills:upload",
        "skills:detail",
        "skills:files",
        "skills:models",
        "skills:usage",
        "skills:delete",
        "skills:download",
    },
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
}

SECTION_URL_NAMES: dict[str, set[str]] = {**_MAIN_SECTION_URL_NAMES, **_SETTINGS_SECTION_URL_NAMES}


def _resolve_active_section(request) -> str:
    # A view may borrow another URL's section — needed where one URL serves rows of
    # several sections (e.g. mcp_connectors:edit renders global AND personal rows).
    match = getattr(request, "resolver_match", None)
    view_name = getattr(request, "nav_view_name", None) or (match.view_name if match else "")
    for section_key, names in SECTION_URL_NAMES.items():
        if view_name in names:
            return section_key
    return ""


def running_snapshot(visible, user) -> dict[str, Any]:
    """The RUNNING rows of ``visible`` (``Run.objects.visible_to(user)``), under the nav stream's keys.

    ``running_runs`` is the badge. ``latest_running_start`` moves whenever another run starts, even when one
    finishing at the same moment leaves the count unchanged; it is kept for session pages loaded before the nav
    stream carried ``session`` frames, which watch it to hear that a run started after they rendered.
    The ``own_`` pair is the same snapshot over the sessions the user owns, which the sidebar's lists show.
    """
    own = Exists(Session.objects.owned_by(user).filter(pk=OuterRef("session_id")))
    stats = visible.filter(status=RunStatus.RUNNING).aggregate(
        running_runs=Count("pk"),
        latest=Max("started_at"),
        own_running_runs=Count("pk", filter=own),
        own_latest=Max("started_at", filter=own),
    )
    return {
        "running_runs": stats["running_runs"],
        "latest_running_start": _isoformat(stats["latest"]),
        "own_running_runs": stats["own_running_runs"],
        "own_latest_running_start": _isoformat(stats["own_latest"]),
    }


def _isoformat(moment) -> str:
    return moment.isoformat() if moment else ""


def query_running_snapshot(user) -> dict[str, Any]:
    """``running_snapshot`` of the runs ``user`` can see.

    The nav's single source of truth: the first page render reads it through ``nav`` below,
    and every subsequent update comes from the SSE endpoint (``dashboard.api.views``)
    recomputing it on a ``core.ui_events`` poke. Raises on ``DatabaseError`` so each caller
    picks its own degradation — a page render shows 0, but a live stream must keep the
    browser's last value rather than push a zero it cannot distinguish from a real one.

    SYNC ONLY — ``visible_to`` resolves platform identity with a DB read at query-build time.
    """
    return running_snapshot(Run.objects.visible_to(user), user)


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
        state = {"running_runs": 0, "latest_running_start": "", "own_running_runs": 0, "own_latest_running_start": ""}
    request._daiv_running_state = state
    return state


def running_jobs_count(request, user) -> int:
    return running_state(request, user)["running_runs"]


def _viewed_thread_id(request) -> str:
    match = getattr(request, "resolver_match", None)
    if match is None or match.view_name != "session_detail":
        return ""
    return match.kwargs.get("thread_id", "")


def sidebar_state(request, user) -> SidebarLists:
    """``sidebar_lists`` memoized per-request via ``request._daiv_sidebar_lists``; empty on ``DatabaseError``."""
    cached = getattr(request, "_daiv_sidebar_lists", None)
    if cached is not None:
        return cached
    try:
        state = sidebar_lists(user, viewing=_viewed_thread_id(request))
    except DatabaseError:
        logger.exception("Failed to compute the sidebar lists for user %s", user.pk)
        state = SidebarLists()
    request._daiv_sidebar_lists = state
    return state


def nav(request) -> dict[str, Any]:
    """Supply the nav's running counts, active section, settings mode and sidebar lists to every authenticated request.

    The running counts and the lists are wrapped in ``SimpleLazyObject`` so their queries run only if the
    template actually references them — non-HTML responses (redirects, HTMX fragments, SSE)
    skip them entirely.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    active_section = _resolve_active_section(request)
    return {
        "nav_running_jobs": SimpleLazyObject(lambda: running_jobs_count(request, user)),
        "nav_own_running_jobs": SimpleLazyObject(lambda: running_state(request, user)["own_running_runs"]),
        "nav_active_section": active_section,
        "nav_settings_mode": active_section in _SETTINGS_SECTION_URL_NAMES,
        "nav_session_thread_id": _viewed_thread_id(request),
        "nav_sidebar": SimpleLazyObject(lambda: sidebar_state(request, user)),
    }
