from __future__ import annotations

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
