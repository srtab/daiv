"""Shared MCPServer builders for tests outside the ``mcp_servers`` package.

Imported as ``from tests.unit_tests.mcp_servers.helpers import ...``, matching
``tests/unit_tests/memory/consolidation_helpers.py``.
"""

from __future__ import annotations

from mcp_servers.models import MCPServer


def make_server(name: str, *, status: str = MCPServer.Status.ACTIVE, scope: str = MCPServer.Scope.GLOBAL, **kwargs):
    kwargs.setdefault("transport", MCPServer.Transport.HTTP)
    kwargs.setdefault("url", f"http://{name}")
    return MCPServer.objects.create(name=name, status=status, scope=scope, **kwargs)


def only_servers(*servers: tuple[str, str]):
    """Replace the builtin rows with exactly ``(name, status)`` pairs, so a test's expected
    diff is stated against a pool it fully controls."""
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    return [make_server(name, status=status) for name, status in servers]
