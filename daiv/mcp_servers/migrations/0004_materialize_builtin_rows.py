from __future__ import annotations

import logging

from django.db import migrations

logger = logging.getLogger("daiv.mcp_servers.migrations")

# Frozen copies of the seed defaults — migrations must not drift with live code
# (mcp_servers/seeds.py is the runtime source; these are the 2026-07 values).
_REMOTE_DEFAULTS = {
    "sentry": "https://mcp.sentry.dev/mcp?disable-skills=seer,docs,project-management",
    "context7": "https://mcp.context7.com/mcp",
}

_DESCRIPTIONS = {
    "sentry": (
        "Official Sentry remote MCP. Requires an 'Authorization' header with value "
        "'Sentry-Bearer <user auth token>' (literal, stored encrypted — or an env_ref to a "
        "variable holding that full string). Add the header, test the connection, then enable. "
        "For on-premise Sentry, point the URL at your own bridge — see the MCP tools documentation."
    ),
    "context7": (
        "Official Context7 remote MCP for library documentation. Works without credentials at "
        "low rate limits; add a CONTEXT7_API_KEY header to raise them."
    ),
}

# Tool names of the stdio packages the supergateway bridges run — the right filter for an
# imported bridge URL. Copied verbatim from the (deleted) code-defined server classes.
_LEGACY_FILTERS = {
    "sentry": [
        "whoami",
        "find_teams",
        "get_issue_tag_values",
        "get_event_attachment",
        "get_replay_details",
        "get_profile_details",
        "get_sentry_resource",
        "find_organizations",
        "find_projects",
        "find_releases",
        "search_events",
        "search_issue",
        "search_issue_events",
    ],
    "context7": ["resolve-library-id", "query-docs"],
}

# Hosted-endpoint (read-only) names — used only when the legacy env kill switch was set,
# i.e. the row is repointed at the remote default.
_REMOTE_FILTERS = {
    "sentry": [
        "whoami",
        "find_organizations",
        "find_projects",
        "get_sentry_resource",
        "search_events",
        "search_issues",
    ],
    "context7": ["resolve-library-id", "query-docs"],
}


def materialize_builtin_rows(apps, schema_editor):
    """Rewrite ``builtin://`` placeholder rows with the *effective* legacy URL.

    No released version ever creates a ``builtin://`` row, so on a normal upgrade this is a
    no-op — the ``post_migrate`` upsert seeds the full remote defaults instead (see CHANGELOG
    upgrade note). This only matters for deployments already tracking an unreleased,
    dashboard-managed row from this same rollout (a ``builtin://`` placeholder written by an
    earlier commit on this branch): for those, it imports the effective legacy URL so they
    keep working instead of silently reverting to the remote default.

    - env URL set (or container default) → import it, keep ``enabled``, write the legacy
      stdio tool filter (correct for a bridge URL).
    - env explicitly ``None`` (the old kill switch) → remote default URL, ``enabled=False``,
      hosted-endpoint tool filter.
    - missing row → skip (fresh install, or a normal upgrade: the post_migrate upsert seeds
      full remote defaults).
    - already-materialised row (non-placeholder URL) → skip (re-run safety, preserves edits).
    """
    from automation.agent.mcp.conf import settings as mcp_conf

    MCPServer = apps.get_model("mcp_servers", "MCPServer")
    effective = {"sentry": mcp_conf.SENTRY_URL, "context7": mcp_conf.CONTEXT7_URL}

    for name in ("sentry", "context7"):
        row = MCPServer.objects.filter(name=name, source="builtin").first()
        if row is None:
            continue
        if not row.url.startswith("builtin://"):
            continue
        url = effective[name]
        if url is None:
            row.url = _REMOTE_DEFAULTS[name]
            row.enabled = False
            row.tool_filter_items = _REMOTE_FILTERS[name]
        else:
            row.url = url
            row.tool_filter_items = _LEGACY_FILTERS[name]
        row.transport = "http"
        row.description = _DESCRIPTIONS[name]
        row.tool_filter_mode = "allow"
        row.save()
        logger.info("Materialized built-in MCP server row %r (url=%s)", name, row.url)


def noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):
    dependencies = [("mcp_servers", "0003_mcpserver_mcp_tool_filter_items_required_when_mode_set")]
    operations = [migrations.RunPython(materialize_builtin_rows, noop_reverse)]
