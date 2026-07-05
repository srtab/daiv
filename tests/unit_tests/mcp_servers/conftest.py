from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_network_tool_sync(monkeypatch):
    """Neutralize the on-save/refresh network probe by default so view tests
    never open a real MCP connection. Tests that assert sync behavior override
    this by monkeypatching ``mcp_servers.views.services.sync_discovered_tools``
    (or test the real ``services.sync_discovered_tools`` directly, as in
    test_services.py)."""
    from mcp_servers import services

    monkeypatch.setattr(services, "sync_discovered_tools", lambda server: {"ok": True, "count": 0})
