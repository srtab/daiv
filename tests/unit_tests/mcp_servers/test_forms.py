from __future__ import annotations

from django import forms

import pytest
from mcp_servers.forms import MCPServerForm


@pytest.mark.django_db
def test_valid_minimal_form_creates_row():
    form = MCPServerForm(
        data={
            "name": "demo",
            "description": "",
            "transport": "http",
            "url": "http://demo.test/mcp",
            "enabled": "on",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
        }
    )
    assert form.is_valid(), form.errors
    obj = form.save()
    assert obj.name == "demo"
    assert obj.transport == "http"
    assert obj.tool_filter_items == []


@pytest.mark.django_db
def test_invalid_name_pattern_rejected():
    form = MCPServerForm(
        data={
            "name": "BAD NAME",
            "transport": "http",
            "url": "http://x",
            "enabled": "on",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
        }
    )
    assert not form.is_valid()
    assert "name" in form.errors


@pytest.mark.django_db
def test_tool_filter_items_required_when_mode_not_none():
    form = MCPServerForm(
        data={
            "name": "demo",
            "transport": "http",
            "url": "http://demo",
            "enabled": "on",
            "tool_filter_mode": "allow",
            "tool_filter_items": "",
        }
    )
    assert not form.is_valid()
    assert "tool_filter_items" in form.errors


@pytest.mark.django_db
def test_tool_filter_items_parsed_from_newline_separated_text():
    form = MCPServerForm(
        data={
            "name": "demo",
            "transport": "http",
            "url": "http://demo.test/mcp",
            "enabled": "on",
            "tool_filter_mode": "allow",
            "tool_filter_items": "search_events\nfind_organizations\n",
        }
    )
    assert form.is_valid(), form.errors
    obj = form.save()
    assert obj.tool_filter_items == ["search_events", "find_organizations"]


def _formset_data(rows, prefix="headers"):
    """Build POST data for a Django formset with N rows."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "50",
    }
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"{prefix}-{i}-{key}"] = value
    return data


@pytest.mark.django_db
def test_header_formset_literal_and_env_ref_roundtrip():
    from mcp_servers.forms import MCPServerHeaderFormSet, build_headers_from_formset

    formset_data = _formset_data([
        {"name": "Authorization", "mode": "literal", "value": "Bearer abc"},
        {"name": "X-Trace", "mode": "env_ref", "value": "TRACE"},
    ])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert formset.is_valid(), formset.errors
    headers = build_headers_from_formset(formset, existing=None)
    assert headers == [
        {"name": "Authorization", "mode": "literal", "value": "Bearer abc"},
        {"name": "X-Trace", "mode": "env_ref", "value": "TRACE"},
    ]


@pytest.mark.django_db
def test_header_formset_blank_literal_value_preserves_existing():
    from mcp_servers.forms import MCPServerHeaderFormSet, build_headers_from_formset

    existing = [{"name": "Authorization", "mode": "literal", "value": "Bearer existing-secret"}]
    # Submitter posts the row but with an empty value — meaning "keep existing".
    formset_data = _formset_data([{"name": "Authorization", "mode": "literal", "value": ""}])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert formset.is_valid(), formset.errors
    merged = build_headers_from_formset(formset, existing=existing)
    assert merged == [{"name": "Authorization", "mode": "literal", "value": "Bearer existing-secret"}]


@pytest.mark.django_db
def test_header_formset_invalid_header_name_rejected():
    from mcp_servers.forms import MCPServerHeaderFormSet

    formset_data = _formset_data([{"name": "bad header!", "mode": "literal", "value": "x"}])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert not formset.is_valid()


@pytest.mark.django_db
def test_header_formset_env_ref_requires_value():
    """An env_ref with no variable name is useless and must be rejected."""
    from mcp_servers.forms import MCPServerHeaderFormSet

    formset_data = _formset_data([{"name": "X-Tok", "mode": "env_ref", "value": ""}])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert not formset.is_valid()


@pytest.mark.django_db
def test_header_formset_deletion_drops_header():
    """A row marked for deletion must not be persisted, even if it has a value."""
    from mcp_servers.forms import MCPServerHeaderFormSet, build_headers_from_formset

    existing = [{"name": "X-Old", "mode": "literal", "value": "secret"}]
    formset_data = _formset_data([{"name": "X-Old", "mode": "literal", "value": "secret", "DELETE": "on"}])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert formset.is_valid(), formset.errors
    assert build_headers_from_formset(formset, existing=existing) == []


@pytest.mark.django_db
def test_header_formset_blank_literal_dropped_on_create():
    """A blank literal with no existing value to preserve (create) is skipped, not persisted empty."""
    from mcp_servers.forms import MCPServerHeaderFormSet, build_headers_from_formset

    formset_data = _formset_data([{"name": "X-Empty", "mode": "literal", "value": ""}])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert formset.is_valid(), formset.errors
    assert build_headers_from_formset(formset, existing=None) == []


@pytest.mark.django_db
def test_reserved_name_rejected():
    """Names that collide with non-slug URL segments are rejected at the form layer."""
    form = MCPServerForm(
        data={"name": "test", "transport": "http", "url": "http://x.test", "enabled": "on", "tool_filter_mode": "none"}
    )
    assert not form.is_valid()
    assert "name" in form.errors


@pytest.mark.django_db
def test_name_cannot_change_on_edit():
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="orig", transport="http", url="http://x.test")
    form = MCPServerForm(
        instance=obj,
        data={
            "name": "renamed",
            "transport": "http",
            "url": "http://x.test",
            "enabled": "on",
            "tool_filter_mode": "none",
            "tool_filter_items": "",
        },
    )
    assert not form.is_valid()
    assert "name" in form.errors


@pytest.mark.django_db
def test_form_renders_textarea_when_no_discovered_tools():
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="demo", transport="http", url="http://demo.test")
    form = MCPServerForm(instance=obj)
    assert isinstance(form.fields["tool_filter_items"], forms.CharField)
    assert isinstance(form.fields["tool_filter_items"].widget, forms.Textarea)


@pytest.mark.django_db
def test_form_renders_checkboxes_when_tools_discovered():
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="demo", transport="http", url="http://demo.test")
    form = MCPServerForm(
        instance=obj,
        discovered_tools=[
            {"name": "search_events", "description": "Find events"},
            {"name": "find_orgs", "description": "Look up orgs"},
        ],
    )
    field = form.fields["tool_filter_items"]
    assert isinstance(field, forms.MultipleChoiceField)
    assert isinstance(field.widget, forms.CheckboxSelectMultiple)
    choices_map = dict(field.choices)
    assert "search_events" in choices_map
    assert "find_orgs" in choices_map


@pytest.mark.django_db
def test_form_with_checkboxes_saves_selected_items():
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="demo", transport="http", url="http://demo.test")
    form = MCPServerForm(
        instance=obj,
        data={
            "name": "demo",
            "transport": "http",
            "url": "http://demo.test",
            "enabled": "on",
            "tool_filter_mode": "allow",
            "tool_filter_items": ["search_events"],
        },
        discovered_tools=[{"name": "search_events", "description": "x"}, {"name": "find_orgs", "description": "y"}],
    )
    assert form.is_valid(), form.errors
    obj = form.save()
    assert obj.tool_filter_items == ["search_events"]


@pytest.mark.django_db
def test_form_persisted_not_discovered_item_still_listed():
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(
        name="demo",
        transport="http",
        url="http://demo.test",
        tool_filter_mode="allow",
        tool_filter_items=["renamed_tool"],
    )
    form = MCPServerForm(instance=obj, discovered_tools=[{"name": "current_tool", "description": ""}])
    choices_map = dict(form.fields["tool_filter_items"].choices)
    # The persisted tool still appears so the admin can un-check it.
    assert "renamed_tool" in choices_map
    # And the label hints that it isn't currently available.
    assert "not in current tool list" in choices_map["renamed_tool"]


@pytest.mark.django_db
def test_form_falls_back_to_textarea_when_discovery_returns_empty():
    """Empty discovered_tools must keep the textarea — a transient discovery failure must not wipe the filter."""
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(
        name="demo", transport="http", url="http://demo.test", tool_filter_mode="allow", tool_filter_items=["t1", "t2"]
    )
    form = MCPServerForm(instance=obj, discovered_tools=[])
    assert isinstance(form.fields["tool_filter_items"], forms.CharField)
    assert isinstance(form.fields["tool_filter_items"].widget, forms.Textarea)
    assert "t1" in form.fields["tool_filter_items"].initial
    assert "t2" in form.fields["tool_filter_items"].initial
