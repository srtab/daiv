"""Tests for the UI agent-run form."""

import json

from django import forms

import pytest
from activity.forms import AgentRunCreateForm, RepoListField
from notifications.choices import NotifyOn


def _valid(**overrides):
    data = {
        "prompt": "do the thing",
        "repos": json.dumps([{"repo_id": "acme/repo", "ref": "main"}]),
        "use_max": False,
        "notify_on": NotifyOn.NEVER,
    }
    data.update(overrides)
    return data


class TestAgentRunCreateForm:
    def test_valid_single_repo(self):
        form = AgentRunCreateForm(data=_valid())
        assert form.is_valid(), form.errors
        assert form.cleaned_data["repos"] == [{"repo_id": "acme/repo", "ref": "main"}]

    def test_valid_multiple_repos(self):
        form = AgentRunCreateForm(
            data=_valid(repos=json.dumps([{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}]))
        )
        assert form.is_valid(), form.errors
        assert len(form.cleaned_data["repos"]) == 2

    def test_rejects_empty_repos(self):
        form = AgentRunCreateForm(data=_valid(repos="[]"))
        assert not form.is_valid()
        assert "repos" in form.errors

    def test_rejects_oversized_repos(self):
        big = [{"repo_id": f"o/r{i}", "ref": ""} for i in range(21)]
        form = AgentRunCreateForm(data=_valid(repos=json.dumps(big)))
        assert not form.is_valid()
        assert "repos" in form.errors

    def test_rejects_malformed_json(self):
        form = AgentRunCreateForm(data=_valid(repos="not-json"))
        assert not form.is_valid()
        assert "repos" in form.errors

    def test_rejects_malformed_entry(self):
        form = AgentRunCreateForm(data=_valid(repos=json.dumps([{"repo_id": ""}])))
        assert not form.is_valid()
        assert "repos" in form.errors

    def test_rejects_duplicate_entries(self):
        form = AgentRunCreateForm(
            data=_valid(repos=json.dumps([{"repo_id": "a/b", "ref": "main"}, {"repo_id": "a/b", "ref": "main"}]))
        )
        assert not form.is_valid()
        assert "repos" in form.errors

    def test_requires_notify_on(self):
        data = _valid()
        data.pop("notify_on")
        form = AgentRunCreateForm(data=data)
        assert not form.is_valid()
        assert "notify_on" in form.errors

    def test_requires_prompt(self):
        form = AgentRunCreateForm(data=_valid(prompt=""))
        assert not form.is_valid()
        assert "prompt" in form.errors


@pytest.mark.parametrize("notify_on", [NotifyOn.NEVER, NotifyOn.ALWAYS, NotifyOn.ON_FAILURE])
def test_notify_on_round_trips(notify_on):
    form = AgentRunCreateForm(data=_valid(notify_on=notify_on))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["notify_on"] == notify_on


class _OptionalRepoForm(forms.Form):
    """Tiny harness — exercises RepoListField(required=False) in isolation."""

    repos = RepoListField(required=False)


class TestRepoListFieldOptional:
    """``required=False`` must accept an empty list but still reject malformed shapes."""

    @pytest.mark.parametrize("payload", ["[]", ""])
    def test_accepts_empty_payloads(self, payload):
        form = _OptionalRepoForm(data={"repos": payload})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["repos"] in ([], None)

    def test_accepts_populated_list(self):
        form = _OptionalRepoForm(data={"repos": json.dumps([{"repo_id": "a/b", "ref": ""}])})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["repos"] == [{"repo_id": "a/b", "ref": ""}]

    @pytest.mark.parametrize("payload", ["{}", '{"repo_id": "a/b"}', '"oops"'])
    def test_rejects_malformed_non_list(self, payload):
        # The required=False short-circuit must only fire for exactly [] — other
        # falsy/malformed shapes must still surface as an explicit error.
        form = _OptionalRepoForm(data={"repos": payload})
        assert not form.is_valid()
        assert "repos" in form.errors
