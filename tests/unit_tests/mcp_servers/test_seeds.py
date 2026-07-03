from __future__ import annotations

from mcp_servers.seeds import BUILTIN_SEEDS


def test_catalog_contains_sentry_and_context7():
    by_name = {s.name: s for s in BUILTIN_SEEDS}
    assert set(by_name) == {"sentry", "context7"}


def test_sentry_seed_defaults():
    sentry = next(s for s in BUILTIN_SEEDS if s.name == "sentry")
    assert sentry.url == "https://mcp.sentry.dev/mcp?disable-skills=seer,docs,project-management"
    assert sentry.enabled is False  # 401s until an auth header is configured
    assert sentry.tool_filter_mode == "allow"
    # Read-only intent: no mutating tools, no meta-dispatch tools.
    assert "update_issue" not in sentry.tool_filter_items
    assert "execute_sentry_tool" not in sentry.tool_filter_items
    assert "Sentry-Bearer" in sentry.description


def test_context7_seed_defaults():
    context7 = next(s for s in BUILTIN_SEEDS if s.name == "context7")
    assert context7.url == "https://mcp.context7.com/mcp"
    assert context7.enabled is True  # keyless works at low rate limits
    assert context7.tool_filter_items == ("resolve-library-id", "query-docs")
