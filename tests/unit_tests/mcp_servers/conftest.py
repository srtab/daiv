from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_network_tool_sync(monkeypatch):
    """Neutralize the refresh view's network probe by default so view tests never
    open a real MCP connection. (Today ``sync_discovered_tools`` is called only by
    ``MCPServerRefreshToolsView``; a later task also calls it on save.)

    Tests that must assert *real* sync behavior opt out one of two ways:

    - per-test, by monkeypatching ``mcp_servers.views.services.sync_discovered_tools``
      (see the refresh-view tests in ``test_views.py``); the per-test patch wins for
      that test and is restored afterwards, or
    - module-wide, in ``test_services.py``, which calls ``services.sync_discovered_tools``
      *directly* and therefore defines its own identically-named autouse override fixture
      that no-ops. That override is NOT dead code — deleting it re-enables this stub for
      that module and silently regresses the two direct sync tests
      (``test_sync_discovered_tools_ok_persists_snapshot`` /
      ``..._failure_preserves_prior_snapshot``). Do not remove it without checking
      test_services.py."""
    from mcp_servers import services

    monkeypatch.setattr(services, "sync_discovered_tools", lambda server: {"ok": True, "count": 0})
