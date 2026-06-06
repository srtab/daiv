from __future__ import annotations

from django.db import IntegrityError

import pytest
from mcp_servers.models import MCPServer


@pytest.mark.django_db
def test_headers_roundtrip_through_encrypted_descriptor():
    server = MCPServer.objects.create(
        name="demo",
        transport=MCPServer.Transport.HTTP,
        url="http://example.test/mcp",
        headers=[
            {"name": "Authorization", "mode": "literal", "value": "Bearer secret"},
            {"name": "X-Trace", "mode": "env_ref", "value": "TRACE_HEADER"},
        ],
    )
    server.refresh_from_db()
    assert server.headers == [
        {"name": "Authorization", "mode": "literal", "value": "Bearer secret"},
        {"name": "X-Trace", "mode": "env_ref", "value": "TRACE_HEADER"},
    ]


@pytest.mark.django_db
def test_name_is_unique():
    MCPServer.objects.create(name="dup", transport=MCPServer.Transport.HTTP, url="http://a")
    with pytest.raises(IntegrityError):
        MCPServer.objects.create(name="dup", transport=MCPServer.Transport.HTTP, url="http://b")


@pytest.mark.django_db
def test_defaults():
    server = MCPServer.objects.create(name="d", transport=MCPServer.Transport.HTTP, url="http://x")
    assert server.source == MCPServer.Source.CUSTOM
    assert server.enabled is True
    assert server.tool_filter_mode == MCPServer.FilterMode.NONE
    assert server.tool_filter_items == []
    assert server.headers is None


@pytest.mark.django_db
@pytest.mark.parametrize("mode", [MCPServer.FilterMode.ALLOW, MCPServer.FilterMode.BLOCK])
def test_check_constraint_rejects_empty_items_when_mode_set(mode):
    """Empty items with allow/block silently inverts intent (allow-nothing / block-nothing)."""
    with pytest.raises(IntegrityError):
        MCPServer.objects.create(
            name="bad", transport=MCPServer.Transport.HTTP, url="http://x", tool_filter_mode=mode, tool_filter_items=[]
        )


@pytest.mark.django_db
def test_check_constraint_allows_none_mode_with_empty_items():
    obj = MCPServer.objects.create(
        name="ok",
        transport=MCPServer.Transport.HTTP,
        url="http://x",
        tool_filter_mode=MCPServer.FilterMode.NONE,
        tool_filter_items=[],
    )
    assert obj.pk is not None
