from __future__ import annotations

from django.urls import reverse

import pytest


@pytest.mark.django_db
def test_list_requires_login(client):
    resp = client.get(reverse("mcp_servers:list"))
    # Anonymous: redirected to login (default LoginRequiredMixin behavior)
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_list_denies_member(client, member_user):
    client.force_login(member_user)
    resp = client.get(reverse("mcp_servers:list"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_list_admin_gets_200(client, admin_user):
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:list"))
    assert resp.status_code == 200


@pytest.mark.django_db
def test_create_get_renders_form(client, admin_user):
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:create"))
    assert resp.status_code == 200
    assert b'name="name"' in resp.content


@pytest.mark.django_db
def test_create_post_creates_server(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:create"),
        data={
            "name": "from-ui",
            "transport": "http",
            "url": "http://from-ui.test/mcp",
            "enabled": "on",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
            "headers-TOTAL_FORMS": "0",
            "headers-INITIAL_FORMS": "0",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
        },
    )
    assert resp.status_code == 302
    from mcp_servers.models import MCPServer

    assert MCPServer.objects.filter(name="from-ui").exists()


@pytest.mark.django_db
def test_create_post_member_denied(client, member_user):
    client.force_login(member_user)
    resp = client.post(reverse("mcp_servers:create"), data={})
    assert resp.status_code == 403
