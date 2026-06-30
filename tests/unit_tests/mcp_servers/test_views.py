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


@pytest.mark.django_db
def test_edit_custom_updates_fields(client, admin_user):
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="ed", transport="http", url="http://old.test")
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=[obj.name]),
        data={
            "name": "ed",
            "transport": "http",
            "url": "http://new.test",
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
    obj.refresh_from_db()
    assert obj.url == "http://new.test"


@pytest.mark.django_db
def test_edit_builtin_only_enabled_is_editable(client, admin_user):
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(
        name="bi", source=MCPServer.Source.BUILTIN, transport="http", url="builtin://bi", enabled=True
    )
    client.force_login(admin_user)
    # Attempt to change url + flip enabled
    resp = client.post(
        reverse("mcp_servers:edit", args=[obj.name]),
        data={
            "name": "bi",
            "transport": "sse",  # smuggled
            "url": "http://attacker.test",  # smuggled
            # enabled deliberately omitted -> should disable
            "tool_filter_mode": "block",  # smuggled
            "tool_filter_items": "x",
            "headers-TOTAL_FORMS": "0",
            "headers-INITIAL_FORMS": "0",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
        },
    )
    assert resp.status_code == 302
    obj.refresh_from_db()
    assert obj.enabled is False  # the one editable field changed
    assert obj.url == "builtin://bi"  # smuggled URL ignored
    assert obj.transport == "http"  # smuggled transport ignored
    assert obj.tool_filter_mode == "none"  # smuggled filter ignored


@pytest.mark.django_db
def test_detail_renders(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="dt", transport="http", url="http://x.test")
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:detail", args=["dt"]))
    assert resp.status_code == 200
    assert b"dt" in resp.content


@pytest.mark.django_db
def test_delete_get_renders_confirm(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="delc", transport="http", url="http://x.test")
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:delete", args=["delc"]))
    assert resp.status_code == 200
    assert b"delc" in resp.content


@pytest.mark.django_db
def test_delete_custom_succeeds(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="del", transport="http", url="http://x.test")
    client.force_login(admin_user)
    resp = client.post(reverse("mcp_servers:delete", args=["del"]))
    assert resp.status_code == 302
    assert not MCPServer.objects.filter(name="del").exists()


@pytest.mark.django_db
def test_delete_builtin_returns_404(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="bi", source=MCPServer.Source.BUILTIN, transport="http", url="builtin://bi")
    client.force_login(admin_user)
    resp = client.post(reverse("mcp_servers:delete", args=["bi"]))
    assert resp.status_code == 404
    assert MCPServer.objects.filter(name="bi").exists()


@pytest.mark.django_db
def test_toggle_flips_enabled(client, admin_user):
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="t", transport="http", url="http://x.test", enabled=True)
    client.force_login(admin_user)
    resp = client.post(reverse("mcp_servers:toggle", args=["t"]))
    assert resp.status_code == 302
    obj.refresh_from_db()
    assert obj.enabled is False
    client.post(reverse("mcp_servers:toggle", args=["t"]))
    obj.refresh_from_db()
    assert obj.enabled is True


@pytest.mark.django_db
def test_test_endpoint_invokes_services_with_payload(client, admin_user, monkeypatch):
    captured = {}

    async def fake_test_connection(payload):
        captured["payload"] = payload
        return {"ok": True, "tools": [{"name": "x", "description": ""}]}

    monkeypatch.setattr("mcp_servers.views.services.test_connection", fake_test_connection)
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:test"),
        data={
            "transport": "http",
            "url": "http://demo.test",
            "headers-TOTAL_FORMS": "1",
            "headers-INITIAL_FORMS": "0",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
            "headers-0-name": "X",
            "headers-0-mode": "literal",
            "headers-0-value": "v",
        },
    )
    assert resp.status_code == 200
    assert captured["payload"]["transport"] == "http"
    assert captured["payload"]["url"] == "http://demo.test"
    assert captured["payload"]["headers"] == [{"name": "X", "mode": "literal", "value": "v"}]
    body = resp.json()
    assert body["ok"] is True
    assert body["tools"][0]["name"] == "x"


@pytest.mark.django_db
def test_tools_endpoint_returns_discovered(client, admin_user, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="t", transport="http", url="http://x.test")

    calls = {"n": 0}

    async def fake_discover(server):
        calls["n"] += 1
        return [{"name": "tool_a", "description": "A"}]

    monkeypatch.setattr("mcp_servers.views.services.discover_tools", fake_discover)
    client.force_login(admin_user)

    r1 = client.get(reverse("mcp_servers:tools", args=["t"]))
    r2 = client.get(reverse("mcp_servers:tools", args=["t"]))
    assert r1.status_code == 200
    assert r1.json()["tools"][0]["name"] == "tool_a"
    # Second call within 60s is cached → discover_tools not invoked again
    assert calls["n"] == 1
    assert r2.json()["tools"] == r1.json()["tools"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "method,url_name,kwargs",
    [
        ("get", "list", {}),
        ("get", "create", {}),
        ("post", "create", {}),
        ("get", "detail", {"name": "demo"}),
        ("get", "edit", {"name": "demo"}),
        ("post", "edit", {"name": "demo"}),
        ("get", "delete", {"name": "demo"}),
        ("post", "delete", {"name": "demo"}),
        ("post", "toggle", {"name": "demo"}),
        ("post", "test", {}),
        ("get", "tools", {"name": "demo"}),
    ],
)
def test_member_forbidden_across_all_endpoints(client, member_user, method, url_name, kwargs):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="demo", transport="http", url="http://x.test")
    client.force_login(member_user)
    url = reverse(f"mcp_servers:{url_name}", kwargs=kwargs)
    fn = getattr(client, method)
    resp = fn(url)
    assert resp.status_code == 403, f"{method.upper()} {url} should be forbidden for members"


@pytest.mark.django_db
def test_edit_get_passes_discovered_tools_into_form(client, admin_user, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="dt3", transport="http", url="http://x.test", tool_filter_mode="allow", tool_filter_items=["alpha"]
    )

    async def fake_discover(server):
        return [{"name": "alpha", "description": "the first letter"}, {"name": "beta", "description": "the second"}]

    monkeypatch.setattr("mcp_servers.views.services.discover_tools", fake_discover)
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:edit", args=["dt3"]))
    assert resp.status_code == 200
    # The form rendered checkboxes (multi-choice), not a textarea.
    assert b'type="checkbox"' in resp.content
    assert b"alpha" in resp.content
    assert b"beta" in resp.content


@pytest.mark.django_db
def test_edit_post_preserves_multiple_checkbox_selections(client, admin_user, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="multi", transport="http", url="http://multi.test", tool_filter_mode="allow", tool_filter_items=["seed"]
    )

    async def fake_discover(server):
        return [
            {"name": "alpha", "description": ""},
            {"name": "beta", "description": ""},
            {"name": "gamma", "description": ""},
        ]

    monkeypatch.setattr("mcp_servers.views.services.discover_tools", fake_discover)
    client.force_login(admin_user)

    resp = client.post(
        reverse("mcp_servers:edit", args=["multi"]),
        data={
            "name": "multi",
            "transport": "http",
            "url": "http://multi.test",
            "enabled": "on",
            "tool_filter_mode": "allow",
            # Multiple values for the same name — the bug was collapsing to ['gamma'] only.
            "tool_filter_items": ["alpha", "beta", "gamma"],
            "headers-TOTAL_FORMS": "0",
            "headers-INITIAL_FORMS": "0",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
        },
    )
    assert resp.status_code == 302

    obj = MCPServer.objects.get(name="multi")
    assert sorted(obj.tool_filter_items) == ["alpha", "beta", "gamma"]


@pytest.mark.django_db
def test_detail_renders_tools_when_discovered(client, admin_user, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="dt2", transport="http", url="http://x.test")

    async def fake_discover(server):
        return [{"name": "alpha", "description": "the first letter"}]

    monkeypatch.setattr("mcp_servers.views.services.discover_tools", fake_discover)
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:detail", args=["dt2"]))
    assert resp.status_code == 200
    assert b"alpha" in resp.content
    assert b"the first letter" in resp.content


@pytest.mark.django_db
def test_edit_post_refuses_when_headers_undecryptable(client, admin_user):
    """POST on a row whose ciphertext can't be decoded must not overwrite it with an empty list."""
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(
        name="locked",
        transport="http",
        url="http://locked.test",
        headers=[{"name": "X-T", "mode": "literal", "value": "secret"}],
    )
    MCPServer.objects.filter(pk=obj.pk).update(_headers_encrypted="not-a-fernet-token")
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=["locked"]),
        data={
            "name": "locked",
            "transport": "http",
            "url": "http://new.test",
            "enabled": "on",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
            "headers-TOTAL_FORMS": "0",
            "headers-INITIAL_FORMS": "0",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
        },
        follow=False,
    )
    assert resp.status_code == 302
    assert reverse("mcp_servers:edit", args=["locked"]) in resp["Location"]
    obj.refresh_from_db()
    assert obj.url == "http://locked.test"
    assert obj._headers_encrypted == "not-a-fernet-token"


@pytest.mark.django_db
def test_tools_endpoint_cache_busted_on_modify(client, admin_user, monkeypatch):
    """A save must invalidate the cached tools snapshot via the ``modified`` stamp in the key."""
    import time

    from django.core.cache import cache

    cache.clear()
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="cb", transport="http", url="http://x.test")
    calls = {"n": 0}

    async def fake_discover(server):
        calls["n"] += 1
        return [{"name": f"t{calls['n']}", "description": ""}]

    monkeypatch.setattr("mcp_servers.views.services.discover_tools", fake_discover)
    client.force_login(admin_user)

    r1 = client.get(reverse("mcp_servers:tools", args=["cb"]))
    assert r1.status_code == 200
    assert calls["n"] == 1

    # int(timestamp()) has 1s resolution; sleep ensures ``modified`` crosses it.
    time.sleep(1.05)
    obj.url = "http://x2.test"
    obj.save()

    r2 = client.get(reverse("mcp_servers:tools", args=["cb"]))
    assert r2.status_code == 200
    assert calls["n"] == 2
    assert r2.json()["tools"][0]["name"] == "t2"


@pytest.mark.django_db
def test_tools_endpoint_degrades_on_undecryptable_headers(client, admin_user):
    """A key-rotation (undecryptable ciphertext) must degrade to an empty list with 200,
    not 500 — matching the detail page's behavior."""
    from django.core.cache import cache

    cache.clear()
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(
        name="rot",
        transport="http",
        url="http://rot.test",
        headers=[{"name": "X-T", "mode": "literal", "value": "secret"}],
    )
    MCPServer.objects.filter(pk=obj.pk).update(_headers_encrypted="not-a-fernet-token")
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:tools", args=["rot"]))
    assert resp.status_code == 200
    assert resp.json()["tools"] == []


@pytest.mark.django_db
def test_detail_builtin_shows_runtime_tools_message(client, admin_user, monkeypatch):
    """A built-in detail page must not run discovery against its placeholder URL and
    must explain that tools are provided at runtime (not 'unreachable')."""
    from mcp_servers.models import MCPServer

    called = {"n": 0}

    async def _should_not_run(payload):
        called["n"] += 1
        return {"ok": True, "tools": []}

    # Patch the network boundary (test_connection); the real discover_tools must short-circuit
    # for built-ins before ever reaching it.
    monkeypatch.setattr("mcp_servers.services.test_connection", _should_not_run)
    MCPServer.objects.create(
        name="bi", source=MCPServer.Source.BUILTIN, transport="http", url="builtin://bi", enabled=True
    )
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:detail", args=["bi"]))
    assert resp.status_code == 200
    assert b"provided directly by their code at runtime" in resp.content
    assert called["n"] == 0  # no doomed handshake against builtin://bi


@pytest.mark.django_db
def test_create_rejects_reserved_name(client, admin_user):
    """Names that collide with non-slug URL segments (e.g. 'new', 'test') are rejected."""
    from mcp_servers.models import MCPServer

    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:create"),
        data={
            "name": "new",
            "transport": "http",
            "url": "http://x.test",
            "enabled": "on",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
            "headers-TOTAL_FORMS": "0",
            "headers-INITIAL_FORMS": "0",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
        },
    )
    assert resp.status_code == 400
    assert not MCPServer.objects.filter(name="new").exists()


@pytest.mark.django_db
def test_list_shows_broken_badge_for_missing_env_ref(client, admin_user, monkeypatch):
    """Enabled row with a missing env-ref must render a 'Broken' badge."""
    from mcp_servers.models import MCPServer

    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
    MCPServer.objects.create(
        name="broken",
        transport="http",
        url="http://broken.test",
        headers=[{"name": "Authorization", "mode": "env_ref", "value": "DEFINITELY_NOT_SET"}],
        enabled=True,
    )
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:list"))
    assert resp.status_code == 200
    assert b"Broken" in resp.content
    assert b"DEFINITELY_NOT_SET" in resp.content


@pytest.mark.django_db
def test_list_does_not_warn_on_disabled_rows(client, admin_user, monkeypatch):
    """Disabled rows are intentionally idle — no broken badge."""
    from mcp_servers.models import MCPServer

    monkeypatch.delenv("ALSO_NOT_SET", raising=False)
    MCPServer.objects.create(
        name="sleeping",
        transport="http",
        url="http://x.test",
        headers=[{"name": "X", "mode": "env_ref", "value": "ALSO_NOT_SET"}],
        enabled=False,
    )
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:list"))
    assert resp.status_code == 200
    assert b"Broken" not in resp.content
