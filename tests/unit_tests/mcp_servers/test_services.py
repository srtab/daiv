from __future__ import annotations

import pytest
from mcp_servers.models import MCPServer
from mcp_servers.services import build_runtime_servers

from automation.agent.mcp.schemas import UserMcpServer


@pytest.mark.django_db
def test_returns_only_enabled_rows():
    MCPServer.objects.create(name="on", transport=MCPServer.Transport.HTTP, url="http://on", enabled=True)
    MCPServer.objects.create(name="off", transport=MCPServer.Transport.HTTP, url="http://off", enabled=False)
    out = build_runtime_servers()
    names = [dto_name for dto_name, _ in out]
    assert names == ["on"]


@pytest.mark.django_db
def test_literal_headers_decrypt_into_dto():
    MCPServer.objects.create(
        name="srv",
        transport=MCPServer.Transport.HTTP,
        url="http://srv",
        headers=[{"name": "Authorization", "mode": "literal", "value": "Bearer abc"}],
    )
    [(name, dto)] = build_runtime_servers()
    assert name == "srv"
    assert isinstance(dto, UserMcpServer)
    assert dto.headers == {"Authorization": "Bearer abc"}
    assert dto.type == "http"
    assert dto.url == "http://srv"
