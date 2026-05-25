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
