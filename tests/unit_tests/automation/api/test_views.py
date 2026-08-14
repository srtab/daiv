"""Tests for automation API views."""

from __future__ import annotations

from django.urls import reverse

import pytest
from mcp_servers.models import MCPServer


@pytest.mark.django_db
def test_mcp_selection_pool_rejects_unauthenticated(client):
    resp = client.get(reverse("api:mcp_selection_pool"))
    assert resp.status_code == 401


@pytest.mark.django_db
def test_mcp_selection_pool_endpoint(client, member_user):
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="g",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://g",
        status=MCPServer.Status.ACTIVE,
    )
    MCPServer.objects.create(
        name="off",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://o",
        status=MCPServer.Status.DISABLED,
    )
    client.force_login(member_user)
    resp = client.get(reverse("api:mcp_selection_pool"))
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()}
    assert "g" in names and "off" not in names


@pytest.mark.django_db
def test_mcp_selection_pool_shape(client, member_user):
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="active_one",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://a",
        status=MCPServer.Status.ACTIVE,
        description="active desc",
    )
    MCPServer.objects.create(
        name="ondemand_one",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://b",
        status=MCPServer.Status.ON_DEMAND,
        description="on-demand desc",
    )
    client.force_login(member_user)
    resp = client.get(reverse("api:mcp_selection_pool"))
    assert resp.status_code == 200
    rows = {r["name"]: r for r in resp.json()}
    assert rows["active_one"]["is_default"] is True
    assert rows["ondemand_one"]["is_default"] is False
    assert {"name", "scope", "description", "is_default"} <= rows["active_one"].keys()
