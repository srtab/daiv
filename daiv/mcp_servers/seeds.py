from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinSeed:
    """Declarative defaults for a built-in MCP server row.

    Consumed by ``mcp_servers.apps.upsert_builtin_rows`` (create-if-missing on
    ``post_migrate``). Existing rows are never overwritten from here — the DB
    row is the source of truth once it exists.
    """

    name: str
    description: str
    url: str
    tool_filter_mode: str
    tool_filter_items: tuple[str, ...]
    enabled: bool


BUILTIN_SEEDS: tuple[BuiltinSeed, ...] = (
    BuiltinSeed(
        name="sentry",
        description=(
            "Official Sentry remote MCP. Requires an 'Authorization' header with value "
            "'Sentry-Bearer <user auth token>' (literal, stored encrypted — or an env_ref to a "
            "variable holding that full string). Add the header, test the connection, then enable. "
            "For on-premise Sentry, point the URL at your own bridge — see the MCP tools documentation."
        ),
        url="https://mcp.sentry.dev/mcp?disable-skills=seer,docs,project-management",
        tool_filter_mode="allow",
        # Read-only tools of the hosted endpoint. Mutating tools (update_issue) and the
        # meta-dispatch pair (search_sentry_tools / execute_sentry_tool — which can reach
        # mutating tools) are deliberately excluded. Unknown names fail closed at runtime.
        tool_filter_items=(
            "whoami",
            "find_organizations",
            "find_projects",
            "get_sentry_resource",
            "search_events",
            "search_issues",
        ),
        enabled=False,
    ),
    BuiltinSeed(
        name="context7",
        description=(
            "Official Context7 remote MCP for library documentation. Works without credentials at "
            "low rate limits; add a CONTEXT7_API_KEY header to raise them."
        ),
        url="https://mcp.context7.com/mcp",
        tool_filter_mode="allow",
        tool_filter_items=("resolve-library-id", "query-docs"),
        enabled=True,
    ),
)
