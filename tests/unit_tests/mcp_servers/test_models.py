from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import Http404

import pytest
from mcp_servers.models import MCPServer


def _global(name="g1", **kw):
    return MCPServer.objects.create(
        name=name,
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="https://example.test/mcp",
        **kw,
    )


def _user_server(user, name="u1", **kw):
    return MCPServer.objects.create(
        name=name,
        scope=MCPServer.Scope.USER,
        user=user,
        transport=MCPServer.Transport.HTTP,
        url="https://example.test/mcp",
        **kw,
    )


@pytest.mark.django_db
def test_global_and_user_may_share_a_name(member_user):
    _global(name="shared")
    # Same name under user scope is allowed (per-scope uniqueness).
    _user_server(member_user, name="shared")


@pytest.mark.django_db
def test_two_global_servers_cannot_share_a_name():
    _global(name="dup")
    with pytest.raises(IntegrityError):
        _global(name="dup")


@pytest.mark.django_db
def test_one_user_cannot_have_two_servers_with_same_name(member_user):
    _user_server(member_user, name="dup")
    with pytest.raises(IntegrityError):
        _user_server(member_user, name="dup")


@pytest.mark.django_db
def test_two_users_may_each_have_same_name(member_user, admin_user):
    _user_server(member_user, name="mine")
    _user_server(admin_user, name="mine")  # different owner → allowed


@pytest.mark.django_db
def test_user_scope_requires_owner():
    with pytest.raises(IntegrityError):
        MCPServer.objects.create(
            name="bad",
            scope=MCPServer.Scope.USER,
            user=None,
            transport=MCPServer.Transport.HTTP,
            url="https://example.test/mcp",
        )


@pytest.mark.django_db
def test_global_scope_forbids_owner(member_user):
    with pytest.raises(IntegrityError):
        MCPServer.objects.create(
            name="bad",
            scope=MCPServer.Scope.GLOBAL,
            user=member_user,
            transport=MCPServer.Transport.HTTP,
            url="https://example.test/mcp",
        )


@pytest.mark.django_db
def test_builtin_must_be_global(member_user):
    with pytest.raises(IntegrityError):
        MCPServer.objects.create(
            name="bad",
            scope=MCPServer.Scope.USER,
            user=member_user,
            source=MCPServer.Source.BUILTIN,
            transport=MCPServer.Transport.HTTP,
            url="https://example.test/mcp",
        )


@pytest.mark.django_db
def test_scoped_get_global_requires_admin(member_user, admin_user):
    g = _global()
    assert MCPServer.objects.scoped_get(admin_user, g.pk) == g
    with pytest.raises(PermissionDenied):
        MCPServer.objects.scoped_get(member_user, g.pk)


@pytest.mark.django_db
def test_scoped_get_user_is_owner_only(member_user, admin_user):
    s = _user_server(member_user)
    assert MCPServer.objects.scoped_get(member_user, s.pk) == s
    # Admins cannot EDIT another user's personal server.
    with pytest.raises(Http404):
        MCPServer.objects.scoped_get(admin_user, s.pk)


@pytest.mark.django_db
def test_manageable_get_user_allows_owner_or_admin(member_user, admin_user):
    s = _user_server(member_user)
    assert MCPServer.objects.manageable_get(member_user, s.pk) == s
    assert MCPServer.objects.manageable_get(admin_user, s.pk) == s  # admin oversight


@pytest.mark.django_db
def test_manageable_get_user_denies_other_member(member_user):
    from accounts.models import Role, User

    other = User.objects.create_user(username="other", email="o@test.com", password="x", role=Role.MEMBER)  # noqa: S106
    s = _user_server(member_user)
    with pytest.raises(Http404):
        MCPServer.objects.manageable_get(other, s.pk)


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
