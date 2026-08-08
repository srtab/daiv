import pytest
from mcp_servers.models import MCPServer
from mcp_servers.selection import PoolEntry, build_selection_pool, default_names, diff_selection, effective_selection

POOL = [
    PoolEntry(name="a", scope="global", description="", is_default=True),
    PoolEntry(name="b", scope="global", description="", is_default=False),
    PoolEntry(name="mine", scope="user", description="", is_default=True),
]


def test_default_names():
    assert default_names(POOL) == {"a", "mine"}


def test_diff_selection_emits_only_deviations():
    # untouched (default set exactly) → {}
    assert diff_selection({"a", "mine"}, POOL) == {}
    # turn a default off, turn an on-demand on
    assert diff_selection({"b"}, POOL) == {"a": "off", "b": "on", "mine": "off"}
    # unknown checked name ignored
    assert diff_selection({"a", "mine", "ghost"}, POOL) == {}


def test_effective_selection_roundtrips_with_diff():
    for selected in ({"a", "mine"}, {"b"}, set(), {"a", "b", "mine"}):
        overrides = diff_selection(selected, POOL)
        assert effective_selection(overrides, POOL) == selected


def test_effective_selection_self_heals_stale_on():
    # "on" for a name no longer in the pool is dropped.
    assert effective_selection({"gone": "on"}, POOL) == {"a", "mine"}


@pytest.mark.django_db
def test_build_selection_pool_marks_defaults_and_excludes_disabled(member_user):
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="g-active",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://g1",
        status=MCPServer.Status.ACTIVE,
    )
    MCPServer.objects.create(
        name="g-ond",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://g2",
        status=MCPServer.Status.ON_DEMAND,
    )
    MCPServer.objects.create(
        name="g-off",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://g3",
        status=MCPServer.Status.DISABLED,
    )
    MCPServer.objects.create(
        name="u1",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="http://u1",
        status=MCPServer.Status.ACTIVE,
    )
    pool = build_selection_pool(member_user.id)
    by_name = {e.name: e for e in pool}
    assert "g-off" not in by_name
    assert by_name["g-active"].is_default is True and by_name["g-active"].scope == "global"
    assert by_name["g-ond"].is_default is False
    assert by_name["u1"].scope == "user" and by_name["u1"].is_default is True
