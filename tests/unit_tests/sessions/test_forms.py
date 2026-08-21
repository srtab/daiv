"""Tests for sessions.forms — the RepoListField parsing branches and the
agent-model server-side backstop in AgentRunFieldsMixin.clean."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.core.exceptions import ValidationError

import pytest
from mcp_servers.models import MCPServer
from sessions.forms import AgentRunCreateForm, MCPSelectionField, RepoListField

from automation.agent.validators import AgentOverrideError
from tests.unit_tests.mcp_servers.helpers import only_servers

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


def test_mcp_selection_field_keeps_absent_distinct_from_deselect_all():
    """`clean()` diffs a submitted selection against the pool, so "nothing was submitted" must not
    arrive as the empty list — that reads as an explicit deselect-all and disables every server."""
    field = MCPSelectionField(required=False)
    assert field.clean("") is None
    assert field.clean(None) is None
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
    data = {"prompt": "go", "repos": json.dumps([{"repo_id": "a/b", "ref": ""}])}
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


def test_agent_run_form_has_no_notify_field(member_user):
    from sessions.forms import AgentRunCreateForm

    form = AgentRunCreateForm(user=member_user)
    assert "notify_on" not in form.fields


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        pytest.param('["b"]', {"a": "off", "b": "on"}, id="retuned"),
        pytest.param('["a"]', {}, id="matches-the-defaults"),
        # `[]` is a real answer — the user unchecked every box — and must still diff.
        pytest.param("[]", {"a": "off"}, id="deselect-all"),
        # Absent (Alpine blocked, a scripted POST) must not read as a deselect-all; with no
        # instance and no `mcp_overrides` kwarg, "what is stored" is the untouched pool.
        pytest.param(None, {}, id="absent"),
    ],
)
def test_run_form_diffs_only_a_submitted_selection(member_user, submitted, expected):
    only_servers(("a", MCPServer.Status.ACTIVE), ("b", MCPServer.Status.ON_DEMAND))
    extra = {} if submitted is None else {"mcp_servers": submitted}

    form = AgentRunCreateForm(data=_form_data(**extra), user=member_user)

    assert form.is_valid(), form.errors
    assert form.cleaned_data["mcp_overrides"] == expected
