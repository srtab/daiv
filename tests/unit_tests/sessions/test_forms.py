"""Tests for sessions.forms — the RepoListField parsing branches and the
agent-model server-side backstop in AgentRunFieldsMixin.clean."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.core.exceptions import ValidationError

import pytest
from sessions.forms import AgentRunCreateForm, MCPSelectionField, RepoListField

from automation.agent.validators import AgentOverrideError

pytestmark = pytest.mark.django_db


def test_repo_list_field_optional_empty_bypasses_validation():
    assert RepoListField(required=False).clean("[]") == []


def test_repo_list_field_required_empty_is_rejected():
    with pytest.raises(ValidationError):
        RepoListField(required=True).clean("[]")


def test_repo_list_field_valid_entries_normalize():
    cleaned = RepoListField(required=False).clean('[{"repo_id": "a/b", "ref": "main"}]')
    assert cleaned == [{"repo_id": "a/b", "ref": "main"}]


def test_repo_list_field_malformed_shape_still_errors_when_optional():
    # Not the exactly-empty list, so it must fall through to validate_repo_list.
    with pytest.raises(ValidationError):
        RepoListField(required=False).clean('[{"repo_id": ""}]')


def test_repo_list_field_prepare_value_serializes_empty_as_bracket():
    field = RepoListField(required=False)
    assert field.prepare_value(None) == "[]"
    assert field.prepare_value([]) == "[]"


def test_mcp_selection_field_normalizes_empty_to_list():
    field = MCPSelectionField(required=False)
    assert field.clean("") == []
    assert field.clean("[]") == []


def test_mcp_selection_field_accepts_list_of_str():
    assert MCPSelectionField(required=False).clean('["a", "b"]') == ["a", "b"]


def test_mcp_selection_field_rejects_non_list():
    with pytest.raises(ValidationError):
        MCPSelectionField(required=False).clean('{"a": 1}')


def test_mcp_selection_field_rejects_non_str_elements():
    with pytest.raises(ValidationError):
        MCPSelectionField(required=False).clean("[1, 2]")


def test_mcp_selection_field_prepare_value_degrades_malformed_string_to_empty():
    field = MCPSelectionField(required=False)
    # A bound-form re-render feeds the raw submitted string back; a malformed one degrades to [].
    assert field.prepare_value("not json") == []
    assert field.prepare_value('["a"]') == ["a"]
    assert field.prepare_value(["a"]) == ["a"]
    assert field.prepare_value(None) == []


def _form_data(**overrides):
    data = {"prompt": "go", "repos": json.dumps([{"repo_id": "a/b", "ref": ""}]), "notify_on": "never"}
    data.update(overrides)
    return data


def test_clean_surfaces_agent_model_backstop_error(member_user):
    """If ensure_agent_model_available rejects (no system default), it is surfaced on agent_model."""
    with patch("sessions.forms.ensure_agent_model_available", side_effect=AgentOverrideError("no default model")):
        form = AgentRunCreateForm(data=_form_data(), user=member_user)
        assert not form.is_valid()
        assert "agent_model" in form.errors


def test_clean_passes_when_agent_model_available(member_user):
    with patch("sessions.forms.ensure_agent_model_available", return_value=None):
        form = AgentRunCreateForm(data=_form_data(), user=member_user)
        assert form.is_valid(), form.errors


def test_run_form_diffs_selection_to_overrides(member_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="a",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://a",
        status=MCPServer.Status.ACTIVE,
    )
    MCPServer.objects.create(
        name="b",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://b",
        status=MCPServer.Status.ON_DEMAND,
    )
    form = AgentRunCreateForm(
        data={
            "prompt": "p",
            "repos": '[{"repo_id": "g/r", "ref": ""}]',
            "notify_on": "never",
            "agent_model": "",
            "agent_thinking_level": "",
            "mcp_servers": '["b"]',
        },
        user=member_user,
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["mcp_overrides"] == {"a": "off", "b": "on"}


def test_run_form_untouched_selection_yields_empty_overrides(member_user):
    from mcp_servers.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="a",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://a",
        status=MCPServer.Status.ACTIVE,
    )
    form = AgentRunCreateForm(
        data={
            "prompt": "p",
            "repos": '[{"repo_id": "g/r", "ref": ""}]',
            "notify_on": "never",
            "agent_model": "",
            "agent_thinking_level": "",
            "mcp_servers": '["a"]',
        },
        user=member_user,
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["mcp_overrides"] == {}
