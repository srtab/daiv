from __future__ import annotations

from django import forms
from django.http import QueryDict

import pytest
from mcp_servers.forms import MCPServerForm, ToolFilterItemsField


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


def _formset_data(rows, prefix="headers", initial_forms=0):
    """Build POST data for a Django formset with N rows.

    ``initial_forms`` sets INITIAL_FORMS: rows at an index below it are
    server-rendered (not ``empty_permitted``) and so are always validated —
    the edit path (``initial=…``) mirrored here.
    """
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial_forms),
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
def test_header_formset_blank_extra_row_is_ignored():
    """A blank row (user clicked 'Add header' then left it empty) must be skipped,
    not rejected. The mode ``<select>`` always submits 'literal', which otherwise
    trips ``has_changed`` and fails ``clean_name`` on the empty header name.
    """
    from mcp_servers.forms import MCPServerHeaderFormSet, build_headers_from_formset

    formset_data = _formset_data([
        {"name": "Authorization", "mode": "literal", "value": "Bearer abc"},
        {"name": "", "mode": "literal", "value": ""},  # blank trailing row
    ])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert formset.is_valid(), formset.errors
    assert build_headers_from_formset(formset, existing=None) == [
        {"name": "Authorization", "mode": "literal", "value": "Bearer abc"}
    ]


@pytest.mark.django_db
def test_header_formset_blank_initial_row_is_rejected_not_ignored():
    """The ``has_changed`` skip is gated on ``empty_permitted``: a blank row at an
    initial index (a server-rendered row the user cleared) must still be rejected,
    not silently dropped. Guards against simplifying the override to an
    unconditional 'blank row is unchanged'.
    """
    from mcp_servers.forms import MCPServerHeaderFormSet

    # INITIAL_FORMS=1 → index 0 is not empty_permitted, so the blank name surfaces.
    formset_data = _formset_data([{"name": "", "mode": "literal", "value": ""}], initial_forms=1)
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert not formset.is_valid()
    assert "name" in formset.errors[0]


@pytest.mark.django_db
def test_header_formset_value_without_name_is_rejected():
    """A value with no header name is a mistake, not an empty row — it must error
    (distinguishes the 'both blank → skip' override from 'value present, name blank').
    """
    from mcp_servers.forms import MCPServerHeaderFormSet

    formset_data = _formset_data([{"name": "", "mode": "literal", "value": "orphan"}])
    formset = MCPServerHeaderFormSet(formset_data, prefix="headers")
    assert not formset.is_valid()
    assert "name" in formset.errors[0]


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
def test_name_widget_readonly_on_edit_not_on_create():
    from mcp_servers.models import MCPServer

    create_form = MCPServerForm()
    assert "readonly" not in create_form.fields["name"].widget.attrs

    obj = MCPServer.objects.create(name="fixed", transport="http", url="http://x.test")
    edit_form = MCPServerForm(instance=obj)
    assert edit_form.fields["name"].widget.attrs.get("readonly")


@pytest.mark.django_db
def test_form_renders_textarea_when_no_discovered_tools():
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(name="demo", transport="http", url="http://demo.test")
    form = MCPServerForm(instance=obj)
    assert isinstance(form.fields["tool_filter_items"], ToolFilterItemsField)
    assert isinstance(form.fields["tool_filter_items"].widget, forms.Textarea)


@pytest.mark.django_db
def test_tools_discovered_produce_checkbox_rows():
    from mcp_servers.forms import build_tool_choices

    rows = build_tool_choices(
        [{"name": "search_events", "description": "Find events"}, {"name": "find_orgs", "description": "Look up orgs"}],
        [],
    )
    names = {r["name"] for r in rows}
    assert {"search_events", "find_orgs"} <= names


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
    )
    assert form.is_valid(), form.errors
    obj = form.save()
    assert obj.tool_filter_items == ["search_events"]


@pytest.mark.django_db
def test_persisted_not_discovered_item_still_listed():
    from mcp_servers.forms import build_tool_choices

    rows = build_tool_choices([{"name": "current_tool", "description": ""}], ["renamed_tool"])
    row = next(r for r in rows if r["name"] == "renamed_tool")
    assert row["available"] is False  # rendered by the template as "not in current tool list"
    assert row["checked"] is True


@pytest.mark.django_db
def test_form_falls_back_to_textarea_when_discovery_returns_empty():
    """Empty discovered_tools must keep the textarea — a transient discovery failure must not wipe the filter."""
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.create(
        name="demo", transport="http", url="http://demo.test", tool_filter_mode="allow", tool_filter_items=["t1", "t2"]
    )
    form = MCPServerForm(instance=obj)
    assert isinstance(form.fields["tool_filter_items"], ToolFilterItemsField)
    assert isinstance(form.fields["tool_filter_items"].widget, forms.Textarea)
    assert "t1" in form.fields["tool_filter_items"].initial
    assert "t2" in form.fields["tool_filter_items"].initial


def _base_form_data(**overrides):
    data = {
        "name": "multi",
        "description": "",
        "transport": "http",
        "url": "http://multi.test/mcp",
        "enabled": "on",
        "tool_filter_mode": "allow",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_tool_filter_items_accepts_repeated_checkbox_values():
    qd = QueryDict(mutable=True)
    qd.update(_base_form_data())
    qd.setlist("tool_filter_items", ["tool_a", "tool_b"])
    form = MCPServerForm(qd)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["tool_filter_items"] == ["tool_a", "tool_b"]


@pytest.mark.django_db
def test_tool_filter_items_accepts_newline_joined_textarea():
    qd = QueryDict(mutable=True)
    qd.update(_base_form_data(tool_filter_items="tool_a\n tool_b \n\n"))
    form = MCPServerForm(qd)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["tool_filter_items"] == ["tool_a", "tool_b"]


@pytest.mark.django_db
def test_tool_filter_items_empty_with_mode_still_errors():
    qd = QueryDict(mutable=True)
    qd.update(_base_form_data(tool_filter_items=""))
    form = MCPServerForm(qd)
    assert not form.is_valid()
    assert "tool_filter_items" in form.errors


def test_build_tool_choices_marks_discovered_and_selected():
    from mcp_servers.forms import build_tool_choices

    rows = build_tool_choices(
        [{"name": "search_events", "description": "Find events"}, {"name": "find_orgs", "description": "Look up orgs"}],
        ["search_events"],
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["search_events"]["checked"] is True
    assert by_name["search_events"]["available"] is True
    assert by_name["search_events"]["description"] == "Find events"
    assert by_name["find_orgs"]["checked"] is False
    assert by_name["find_orgs"]["available"] is True


def test_build_tool_choices_carries_read_only_hint():
    """readOnlyHint flows through tri-state; unannotated and not-discovered rows are None."""
    from mcp_servers.forms import build_tool_choices

    rows = build_tool_choices(
        [
            {"name": "reader", "description": "", "read_only": True},
            {"name": "writer", "description": "", "read_only": False},
            {"name": "unknown", "description": ""},  # server didn't annotate
        ],
        ["gone"],  # selected but not discovered
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["reader"]["read_only"] is True
    assert by_name["writer"]["read_only"] is False
    assert by_name["unknown"]["read_only"] is None
    assert by_name["gone"]["read_only"] is None
    assert by_name["gone"]["available"] is False


def test_build_tool_choices_appends_persisted_not_discovered_last():
    from mcp_servers.forms import build_tool_choices

    rows = build_tool_choices([{"name": "current_tool", "description": ""}], ["renamed_tool"])
    by_name = {r["name"]: r for r in rows}
    assert by_name["renamed_tool"]["available"] is False
    assert by_name["renamed_tool"]["checked"] is True
    assert rows[-1]["name"] == "renamed_tool"  # extras appended after discovered


def test_build_tool_choices_empty_when_no_discovery():
    """No live list → empty result, so the caller falls back to the textarea and
    never wipes a persisted allow/block filter."""
    from mcp_servers.forms import build_tool_choices

    assert build_tool_choices([], ["t1", "t2"]) == []
    assert build_tool_choices(None, ["t1"]) == []


def test_build_tool_choices_skips_empty_and_deduplicates():
    from mcp_servers.forms import build_tool_choices

    rows = build_tool_choices(
        [{"name": "a", "description": "x"}, {"name": ""}, {"name": "a", "description": "dup"}], ["a", "a", "extra"]
    )
    names = [r["name"] for r in rows]
    assert names == ["a", "extra"]  # blank dropped, "a" deduped, extra appended once
