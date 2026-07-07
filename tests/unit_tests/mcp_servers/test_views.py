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
def test_create_get_transport_control_has_no_blank_pill(client, admin_user):
    """``MCPServer.transport`` has no model default, so Django's ``formfield()``
    injects a blank ``('', '---------')`` choice (``include_blank`` is set
    whenever a choices field has no default, regardless of ``blank=False``).
    The segmented-pill control must not render that as a clickable pill —
    unlike the old ``<select>``, a blank ``<label>`` pill here is empty but
    still visible and clickable. Assert the transport radio group renders
    exactly the two real ``MCPServer.Transport`` choices, never a blank one.
    """
    from mcp_servers.models import MCPServer

    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:create"))
    assert resp.status_code == 200
    content = resp.content
    # Exactly one radio per real transport choice — a blank choice would add a third.
    assert content.count(b'name="transport"') == len(MCPServer.Transport.choices)
    for value, _label in MCPServer.Transport.choices:
        assert f'value="{value}"'.encode() in content
    # No transport radio with an empty value (the injected blank choice).
    assert b'name="transport" value=""' not in content


@pytest.mark.django_db
def test_create_get_exposes_add_header_affordance(client, admin_user):
    """The Headers section must ship an empty-form template + an add-row control.

    With ``extra=0`` and no initial rows, the create form renders zero header
    forms; without an ``empty_form`` template and an "Add header" button the
    section is permanently blank and uneditable.
    """
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:create"))
    assert resp.status_code == 200
    # The empty-form template JS clones to create new rows.
    assert b"headers-__prefix__-name" in resp.content
    # A user-visible control to add a header row.
    assert b"Add header" in resp.content


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
def test_edit_get_renders_existing_headers_blanked_and_marked(client, admin_user):
    """Edit GET renders each stored header as a server-rendered (``data-initial``)
    row with the header name shown, and blanks the literal value so the stored
    secret is never echoed into the HTML. A blanked literal value input carries a
    "keep existing" placeholder so the empty box reads as "stored", not "unset".
    An ``env_ref`` value is not a secret: it is shown verbatim and gets no such
    placeholder.
    """
    import re

    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="hdrs",
        transport="http",
        url="http://x.test",
        headers=[
            {"name": "Authorization", "mode": "literal", "value": "Bearer super-secret"},
            {"name": "X-Env", "mode": "env_ref", "value": "MY_TOKEN"},
        ],
    )
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:edit", args=["hdrs"]))
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "data-initial" in body  # server-rendered row marker the JS remove() relies on
    assert "Authorization" in body
    assert "Bearer super-secret" not in body  # literal value blanked, not leaked
    # The blanked literal advertises that a value is stored; the env_ref name shows verbatim.
    assert "keep existing" in body
    assert "MY_TOKEN" in body
    # The placeholder lands on a header value input (the blanked literal), and only there:
    # the env_ref value input, which shows its name, is not marked.
    value_inputs = re.findall(r"<input[^>]*\bname=\"headers-\d+-value\"[^>]*>", body)
    assert len(value_inputs) == 2
    assert sum("keep existing" in tag for tag in value_inputs) == 1
    # Header values are secret-ish: browser autofill/save is disabled on every value input.
    assert all('autocomplete="off"' in tag for tag in value_inputs)


@pytest.mark.django_db
def test_edit_post_adds_removes_and_preserves_headers(client, admin_user):
    """The edit round-trip through the view: a blank literal preserves the stored
    (encrypted) value, a DELETE'd row is dropped, and a new row is added — all in
    one POST with INITIAL_FORMS>0. This is the primary user story of the change.
    """
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(
        name="rt",
        transport="http",
        url="http://rt.test",
        headers=[
            {"name": "Authorization", "mode": "literal", "value": "keep-secret"},
            {"name": "X-Old", "mode": "literal", "value": "drop-me"},
        ],
    )
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=["rt"]),
        data={
            "name": "rt",
            "transport": "http",
            "url": "http://rt.test",
            "enabled": "on",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
            "headers-TOTAL_FORMS": "3",
            "headers-INITIAL_FORMS": "2",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
            # index 0: existing literal, blank value → preserve stored secret
            "headers-0-name": "Authorization",
            "headers-0-mode": "literal",
            "headers-0-value": "",
            # index 1: existing header marked for deletion → dropped
            "headers-1-name": "X-Old",
            "headers-1-mode": "literal",
            "headers-1-value": "",
            "headers-1-DELETE": "on",
            # index 2: brand-new added row → persisted
            "headers-2-name": "X-New",
            "headers-2-mode": "literal",
            "headers-2-value": "fresh",
        },
    )
    assert resp.status_code == 302
    obj.refresh_from_db()
    assert obj.headers == [
        {"name": "Authorization", "mode": "literal", "value": "keep-secret"},
        {"name": "X-New", "mode": "literal", "value": "fresh"},
    ]


@pytest.mark.django_db
def test_edit_builtin_full_form_persists(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="bi", source=MCPServer.Source.BUILTIN, transport="http", url="https://mcp.sentry.dev/mcp", enabled=True
    )
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=["bi"]),
        data={
            "name": "bi",
            "description": "repointed at on-prem bridge",
            "transport": "http",
            "url": "https://bridge.internal/mcp",
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
    obj = MCPServer.objects.get(name="bi")
    assert obj.url == "https://bridge.internal/mcp"
    assert obj.description == "repointed at on-prem bridge"
    assert obj.source == MCPServer.Source.BUILTIN  # source untouched


@pytest.mark.django_db
def test_edit_builtin_rename_rejected(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="bi", source=MCPServer.Source.BUILTIN, transport="http", url="https://mcp.sentry.dev/mcp", enabled=True
    )
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=["bi"]),
        data={
            "name": "renamed",
            "transport": "http",
            "url": "https://mcp.sentry.dev/mcp",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
            "headers-TOTAL_FORMS": "0",
            "headers-INITIAL_FORMS": "0",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
        },
    )
    assert resp.status_code == 400
    assert MCPServer.objects.filter(name="bi").exists()


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
@pytest.mark.parametrize(
    "method,url_name,kwargs",
    [
        ("get", "list", {}),
        ("get", "create", {}),
        ("post", "create", {}),
        ("get", "edit", {"name": "demo"}),
        ("post", "edit", {"name": "demo"}),
        ("get", "delete", {"name": "demo"}),
        ("post", "delete", {"name": "demo"}),
        ("post", "toggle", {"name": "demo"}),
        ("post", "test", {}),
        ("post", "refresh_tools", {"name": "demo"}),
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
def test_edit_get_passes_discovered_tools_into_form(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="dt3",
        transport="http",
        url="http://x.test",
        tool_filter_mode="allow",
        tool_filter_items=["alpha"],
        discovered_tools=[
            {"name": "alpha", "description": "the first letter"},
            {"name": "beta", "description": "the second"},
        ],
    )
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:edit", args=["dt3"]))
    assert resp.status_code == 200
    assert b'type="checkbox"' in resp.content
    assert b"alpha" in resp.content
    assert b"beta" in resp.content
    assert b'data-tool-name="alpha"' in resp.content


@pytest.mark.django_db
def test_edit_get_renders_read_only_pills(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="pills",
        transport="http",
        url="http://p.test",
        tool_filter_mode="allow",
        tool_filter_items=["reader"],
        discovered_tools=[
            {"name": "reader", "description": "", "read_only": True},
            {"name": "writer", "description": "", "read_only": False},
            {"name": "unknown", "description": "", "read_only": None},
        ],
    )
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:edit", args=["pills"]))
    assert resp.status_code == 200
    assert b"mcp-tool-row__pill--ro" in resp.content
    assert b"mcp-tool-row__pill--rw" in resp.content
    assert resp.content.count(b"mcp-tool-row__pill--") == 2


@pytest.mark.django_db
def test_edit_post_preserves_multiple_checkbox_selections(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="multi",
        transport="http",
        url="http://multi.test",
        tool_filter_mode="allow",
        tool_filter_items=["seed"],
        discovered_tools=[
            {"name": "alpha", "description": ""},
            {"name": "beta", "description": ""},
            {"name": "gamma", "description": ""},
        ],
    )
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=["multi"]),
        data={
            "name": "multi",
            "transport": "http",
            "url": "http://multi.test",
            "enabled": "on",
            "tool_filter_mode": "allow",
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
def test_edit_post_triggers_tool_sync(client, admin_user, monkeypatch):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="synced", transport="http", url="http://old.test")
    called = {}

    def fake_sync(server):
        called["name"] = server.name
        return {"ok": True, "count": 0}

    monkeypatch.setattr("mcp_servers.views.services.sync_discovered_tools", fake_sync)
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=["synced"]),
        data={
            "name": "synced",
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
    assert called["name"] == "synced"


@pytest.mark.django_db
def test_create_post_triggers_tool_sync(client, admin_user, monkeypatch):
    from mcp_servers.models import MCPServer

    called = {}

    def fake_sync(server):
        called["name"] = server.name
        return {"ok": True, "count": 0}

    monkeypatch.setattr("mcp_servers.views.services.sync_discovered_tools", fake_sync)
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:create"),
        data={
            "name": "synced-create",
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
    assert MCPServer.objects.filter(name="synced-create").exists()
    assert called["name"] == "synced-create"


@pytest.mark.django_db
def test_edit_post_warns_when_sync_fails(client, admin_user, monkeypatch):
    """A failed on-save probe adds a soft warning flash but the save still succeeds."""
    from django.contrib.messages import get_messages

    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="warned", transport="http", url="http://old.test")
    monkeypatch.setattr("mcp_servers.views.services.sync_discovered_tools", lambda s: {"ok": False, "error": "boom"})
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:edit", args=["warned"]),
        data={
            "name": "warned",
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
    assert resp.status_code == 302  # save still succeeded
    obj = MCPServer.objects.get(name="warned")
    assert obj.url == "http://new.test"  # the edit persisted
    msgs = [(m.level_tag, str(m)) for m in get_messages(resp.wsgi_request)]
    assert any(tag == "warning" and "boom" in text for tag, text in msgs)


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


@pytest.mark.django_db
def test_test_endpoint_reuses_stored_headers_for_existing_server(client, admin_user, monkeypatch):
    """Re-testing a saved server must not drop a preserved (blanked) literal secret header —
    the edit form always blanks literal values on render, so a straight passthrough of the
    formset would probe without the real header and report a false failure."""
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="rt-test",
        transport="http",
        url="http://rt-test.test",
        headers=[{"name": "Authorization", "mode": "literal", "value": "real-secret"}],
    )
    captured = {}

    async def fake_test_connection(payload):
        captured["payload"] = payload
        return {"ok": True, "tools": []}

    monkeypatch.setattr("mcp_servers.views.services.test_connection", fake_test_connection)
    client.force_login(admin_user)
    resp = client.post(
        reverse("mcp_servers:test"),
        data={
            "name": "rt-test",
            "transport": "http",
            "url": "http://rt-test.test",
            "headers-TOTAL_FORMS": "1",
            "headers-INITIAL_FORMS": "1",
            "headers-MIN_NUM_FORMS": "0",
            "headers-MAX_NUM_FORMS": "50",
            "headers-0-name": "Authorization",
            "headers-0-mode": "literal",
            "headers-0-value": "",
        },
    )
    assert resp.status_code == 200
    assert captured["payload"]["headers"] == [{"name": "Authorization", "mode": "literal", "value": "real-secret"}]


def test_create_form_renders_test_connection_button(client, admin_user):
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:create"))
    assert resp.status_code == 200
    assert b"mcpTestConnection" in resp.content
    assert b"mcp-server-form.js" in resp.content


@pytest.mark.django_db
def test_refresh_tools_triggers_sync_and_redirects(client, admin_user, monkeypatch):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="rf-ok", transport="http", url="http://x.test")
    called = {}

    def fake_sync(server):
        called["name"] = server.name
        return {"ok": True, "count": 3}

    monkeypatch.setattr("mcp_servers.views.services.sync_discovered_tools", fake_sync)
    client.force_login(admin_user)
    resp = client.post(reverse("mcp_servers:refresh_tools", args=["rf-ok"]))
    assert resp.status_code == 302
    assert resp.url == reverse("mcp_servers:list")
    assert called["name"] == "rf-ok"


@pytest.mark.django_db
def test_refresh_tools_next_edit_redirects_to_edit(client, admin_user, monkeypatch):
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="rf-edit", transport="http", url="http://x.test")
    monkeypatch.setattr("mcp_servers.views.services.sync_discovered_tools", lambda s: {"ok": True, "count": 0})
    client.force_login(admin_user)
    resp = client.post(reverse("mcp_servers:refresh_tools", args=["rf-edit"]), data={"next": "edit"})
    assert resp.status_code == 302
    assert resp.url == reverse("mcp_servers:edit", args=["rf-edit"])


@pytest.mark.django_db
def test_refresh_tools_reports_sync_failure(client, admin_user, monkeypatch):
    """A failed sync (``ok=False``) flashes an error and still redirects to the list
    without crashing — exercises the view's ``messages.error`` branch."""
    from django.contrib.messages import get_messages

    from mcp_servers.models import MCPServer

    MCPServer.objects.create(name="rf-bad", transport="http", url="http://x.test")
    monkeypatch.setattr("mcp_servers.views.services.sync_discovered_tools", lambda s: {"ok": False, "error": "boom"})
    client.force_login(admin_user)
    resp = client.post(reverse("mcp_servers:refresh_tools", args=["rf-bad"]))
    assert resp.status_code == 302
    assert resp.url == reverse("mcp_servers:list")
    stored = list(get_messages(resp.wsgi_request))
    assert any(m.level_tag == "error" for m in stored)


@pytest.mark.django_db
def test_list_renders_exposed_tool_pills(client, admin_user):
    from django.utils import timezone

    from mcp_servers.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="withtools",
        transport="http",
        url="http://x.test",
        enabled=True,
        discovered_tools=[
            {"name": "create_pr", "description": "", "read_only": False},
            {"name": "get_file", "description": "", "read_only": True},
        ],
        tools_synced_at=timezone.now(),
    )
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:list"))
    assert resp.status_code == 200
    assert b"create_pr" in resp.content
    assert b"get_file" in resp.content
    assert b"mcp-tool-pill" in resp.content


@pytest.mark.django_db
def test_list_shows_not_synced_when_never_synced(client, admin_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(name="nosync", transport="http", url="http://x.test", enabled=True)
    client.force_login(admin_user)
    resp = client.get(reverse("mcp_servers:list"))
    assert b"not synced yet" in resp.content.lower()
