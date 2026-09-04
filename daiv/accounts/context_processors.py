from __future__ import annotations

import logging
from typing import Any

from django.db import Error as DatabaseError
from django.utils.functional import SimpleLazyObject
from django.utils.translation import gettext_lazy as _

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
    "platform_credential": {"platform_credential", "platform_credential_revoke"},
    "users": {"user_list", "user_create", "user_update", "user_delete"},
    "configuration": {"site_configuration", "site_configuration_index"},
    "cross_project_access": {"codebase:cross-project-access"},
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

# The mobile tab bar (< md), where the sidebar is a sheet. Four sections only — the bar is a
# shortcut, not the whole nav — and the section keys are the ones above, so a tab can never
# highlight a section that does not exist.
NAV_TABS: tuple[dict[str, Any], ...] = (
    {"section": "dashboard", "url_name": "dashboard", "icon": "squares-2x2", "label": _("Dashboard")},
    {"section": "sessions", "url_name": "session_list", "icon": "chat-bubble", "label": _("Sessions")},
    {"section": "schedules", "url_name": "schedule_list", "icon": "clock", "label": _("Schedules")},
    {"section": "memory", "url_name": "memory:list", "icon": "cpu-chip", "label": _("Memory")},
)


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


def visible_runs(user):
    """The runs ``user`` may see, as a queryset.

    Split from the count so a long-lived reader (the nav SSE stream) resolves platform
    identity once per connection instead of once per recount — the same hoist
    ``SessionStreamView._stream`` documents.

    SYNC ONLY — ``visible_to`` resolves that identity with a DB read at query-build time;
    async callers must wrap this in ``sync_to_async``.
    """
    from sessions.models import Run  # local import to avoid circulars

    return Run.objects.visible_to(user)


def count_running(visible) -> int:
    """RUNNING rows in a queryset from ``visible_runs``.

    ``values("pk")`` keeps the DISTINCT of the visibility join off every Run column.
    """
    from sessions.models import RunStatus  # local import to avoid circulars

    return visible.filter(status=RunStatus.RUNNING).values("pk").count()


def query_running_jobs(user) -> int:
    """Count the runs ``user`` can see that are currently RUNNING.

    The nav badge's single source of truth: the first page render reads it through
    ``nav`` below, and every subsequent update comes from the SSE endpoint
    (``accounts.api.views``) recomputing it on a ``core.ui_events`` poke. Raises on
    ``DatabaseError`` so each caller picks its own degradation — a page render shows 0,
    but a live stream must keep the browser's last value rather than push a zero it
    cannot distinguish from a real one.

    SYNC ONLY — see ``visible_runs``.
    """
    return count_running(visible_runs(user))


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

    from codebase.conf import settings as codebase_settings

    return {
        "nav_running_jobs": SimpleLazyObject(lambda: running_jobs_count(request, user)),
        "nav_active_section": _resolve_active_section(request),
        "nav_tabs": NAV_TABS,
        "git_platform": codebase_settings.CLIENT.value,
    }


def social_consent(request) -> dict[str, Any]:
    """What the sign-in page must say about the authorisation it is about to request (FR-007).

    The wider scope is requested and the token stored on every sign-in, whether or not
    cross-project access is switched on, so the disclosure tracks the **grant** and not the
    capability — gating it on the toggle would stay silent in exactly the case where a token is
    collected that nothing will use.

    Separate from :func:`nav`, which returns nothing for an anonymous request — and the sign-in
    page is the one place where the person is not signed in yet.
    """
    from codebase.base import GitPlatform
    from codebase.conf import settings as codebase_settings

    platform = codebase_settings.CLIENT
    if platform not in (GitPlatform.GITLAB, GitPlatform.GITHUB):
        return {}
    return {"socialaccount_platform": platform.value.capitalize(), "socialaccount_discloses_wider_grant": True}
