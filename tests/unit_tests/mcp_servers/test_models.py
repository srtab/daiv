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


@pytest.mark.django_db
def test_delete_raises_for_builtin_row():
    """Model-level backstop: the view protects built-ins via a queryset filter, but any
    other caller (shell, management command) must not be able to delete one either."""
    obj = MCPServer.objects.create(name="bi", source=MCPServer.Source.BUILTIN, transport="http", url="http://x")
    with pytest.raises(ValueError):
        obj.delete()
    assert MCPServer.objects.filter(name="bi").exists()


@pytest.mark.django_db
def test_delete_succeeds_for_custom_row():
    obj = MCPServer.objects.create(name="cu", transport="http", url="http://x")
    obj.delete()
    assert not MCPServer.objects.filter(name="cu").exists()


@pytest.mark.django_db
def test_save_raises_on_rename():
    """Model-level backstop for the rename guard MCPServerForm.clean_name already enforces —
    name is a stable key (URLs, cache keys, tool-filter prefix) for any caller, not just forms."""
    obj = MCPServer.objects.create(name="orig", transport="http", url="http://x")
    obj.name = "renamed"
    with pytest.raises(ValueError):
        obj.save()
    assert MCPServer.objects.filter(name="orig").exists()
    assert not MCPServer.objects.filter(name="renamed").exists()


@pytest.mark.django_db
def test_save_allows_unrelated_field_update():
    obj = MCPServer.objects.create(name="orig2", transport="http", url="http://x")
    obj.url = "http://y"
    obj.save()
    obj.refresh_from_db()
    assert obj.url == "http://y"


@pytest.mark.django_db
def test_new_server_has_empty_tool_snapshot():
    s = MCPServer.objects.create(name="snap1", transport=MCPServer.Transport.HTTP, url="http://snap1.test")
    assert s.discovered_tools == []
    assert s.tools_synced_at is None
