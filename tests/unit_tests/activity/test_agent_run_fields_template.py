"""Render tests for ``activity/_agent_run_fields.html``.

Alpine/HTMX behavior (popover opening, picker fetches, chip interactions) is
verified manually in the browser, not here.
"""

from __future__ import annotations

import re

from django.template.loader import render_to_string
from django.urls import reverse

from activity.forms import AgentRunCreateForm


def _render(form):
    return render_to_string("activity/_agent_run_fields.html", {"form": form})


def _input_tag(html, name):
    match = re.search(rf'<input[^>]*\bname="{re.escape(name)}"[^>]*>', html)
    assert match, f"no <input name={name!r}> in rendered HTML"
    return match.group(0)


def test_renders_single_hidden_repos_input():
    form = AgentRunCreateForm(initial={"prompt": "p", "repos": [{"repo_id": "acme/api", "ref": "main"}]})
    html = _render(form)
    tag = _input_tag(html, "repos")
    assert "acme/api" in tag
    assert re.search(r'<input[^>]*\bname="repo_id"', html) is None
    assert re.search(r'<input[^>]*\bname="ref"', html) is None


def test_max_repos_is_twenty():
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert "maxRepos: 20" in html


def test_renders_textarea_with_prompt_value():
    form = AgentRunCreateForm(initial={"prompt": "hello world"})
    html = _render(form)
    assert 'name="prompt"' in html
    assert ">hello world</textarea>" in html


def test_renders_use_max_hidden_false_and_checkbox_checked_when_set():
    form = AgentRunCreateForm(initial={"prompt": "p", "use_max": True})
    html = _render(form)
    assert '<input type="hidden" name="use_max" value="false"' in html
    assert 'name="use_max" value="true"' in html
    assert "checked" in html


def test_renders_use_max_checkbox_unchecked_when_not_set():
    form = AgentRunCreateForm(initial={"prompt": "p", "use_max": False})
    html = _render(form)
    assert '<input type="hidden" name="use_max" value="false"' in html
    assert 'name="use_max" value="true"' in html
    assert " checked" not in html


def test_required_guard_has_value_when_repos_present():
    form = AgentRunCreateForm(initial={"prompt": "p", "repos": [{"repo_id": "x/y", "ref": ""}]})
    html = _render(form)
    assert 'value="ok"' in _input_tag(html, "__repo_required_guard")


def test_required_guard_empty_when_no_repos():
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert 'value="ok"' not in _input_tag(html, "__repo_required_guard")


def test_empty_repos_renders_as_json_array_not_null():
    # Alpine parses the hidden input value as JSON to seed initialRepos; "null" would break boot.
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert 'value="[]"' in _input_tag(html, "repos")


def test_renders_combined_error_list_below_box():
    form = AgentRunCreateForm(data={"prompt": "", "repos": ""})
    form.is_valid()
    html = _render(form)
    assert re.search(r"<ul[^>]*text-red-400", html)
    assert "required" in html.lower()


def test_empty_state_shows_choose_repository_button():
    """With no repo bound, the chip row renders the 'Choose repository' empty-state button."""
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert "Choose repository" in html


def test_repo_picker_popover_uses_picker_url():
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert reverse("codebase:picker-repositories") in html
    assert "x-combobox" not in html


def test_branch_picker_template_references_branches_url_prefix():
    """The branch popover builds its hx-get URL in Alpine; the literal URL prefix must appear in the template."""
    form = AgentRunCreateForm(initial={"prompt": "p", "repos": [{"repo_id": "acme/api", "ref": "main"}]})
    html = _render(form)
    assert "/codebase/pickers/repositories/" in html
    assert "/branches/" in html
