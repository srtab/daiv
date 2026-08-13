from __future__ import annotations

from django.test import RequestFactory

import pytest
from mcp_servers.filters import MCPServerFilter
from mcp_servers.models import MCPServer


def _user_server(user, name="u1"):
    return MCPServer.objects.create(
        name=name, scope=MCPServer.Scope.USER, user=user, transport=MCPServer.Transport.HTTP, url="https://u.test/mcp"
    )


def _filterset(data, user):
    request = RequestFactory().get("/dashboard/mcp-servers/", data)
    request.user = user
    return MCPServerFilter(data, queryset=MCPServer.objects.filter(scope=MCPServer.Scope.USER), request=request)


@pytest.mark.django_db
def test_admin_defaults_to_own_servers(admin_user, member_user):
    mine = _user_server(admin_user, "mine")
    _user_server(member_user, "theirs")
    fs = _filterset({}, admin_user)
    assert list(fs.qs) == [mine]


@pytest.mark.django_db
def test_admin_can_select_another_owner(admin_user, member_user):
    _user_server(admin_user, "mine")
    theirs = _user_server(member_user, "theirs")
    fs = _filterset({"owner": str(member_user.pk)}, admin_user)
    assert list(fs.qs) == [theirs]


@pytest.mark.django_db
def test_member_has_no_owner_filter_and_param_is_inert(member_user, admin_user):
    mine = _user_server(member_user, "mine")
    _user_server(admin_user, "theirs")
    fs = _filterset({"owner": str(admin_user.pk)}, member_user)
    assert "owner" not in fs.filters
    assert list(fs.qs) == [mine]


@pytest.mark.django_db
def test_invalid_owner_value_falls_back_to_own(admin_user, member_user):
    mine = _user_server(admin_user, "mine")
    _user_server(member_user, "theirs")
    fs = _filterset({"owner": "999999"}, admin_user)
    assert list(fs.qs) == [mine]
