"""Tests for the UI agent-run form using the repos_json representation."""

import json

import pytest
from activity.forms import AgentRunCreateForm
from notifications.choices import NotifyOn


def _valid(**overrides):
    data = {
        "prompt": "do the thing",
        "repos_json": json.dumps([{"repo_id": "acme/repo", "ref": "main"}]),
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
            data=_valid(repos_json=json.dumps([{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}]))
        )
        assert form.is_valid(), form.errors
        assert len(form.cleaned_data["repos"]) == 2

    def test_rejects_empty_repos(self):
        form = AgentRunCreateForm(data=_valid(repos_json="[]"))
        assert not form.is_valid()
        assert "repos_json" in form.errors

    def test_rejects_oversized_repos(self):
        big = [{"repo_id": f"o/r{i}", "ref": ""} for i in range(21)]
        form = AgentRunCreateForm(data=_valid(repos_json=json.dumps(big)))
        assert not form.is_valid()
        assert "repos_json" in form.errors

    def test_rejects_malformed_json(self):
        form = AgentRunCreateForm(data=_valid(repos_json="not-json"))
        assert not form.is_valid()
        assert "repos_json" in form.errors

    def test_rejects_malformed_entry(self):
        form = AgentRunCreateForm(data=_valid(repos_json=json.dumps([{"repo_id": ""}])))
        assert not form.is_valid()
        assert "repos_json" in form.errors

    def test_rejects_duplicate_entries(self):
        form = AgentRunCreateForm(
            data=_valid(repos_json=json.dumps([{"repo_id": "a/b", "ref": "main"}, {"repo_id": "a/b", "ref": "main"}]))
        )
        assert not form.is_valid()
        assert "repos_json" in form.errors

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
