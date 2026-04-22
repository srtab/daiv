"""Render tests for ``activity/_agent_run_fields.html``.

Covers the server-rendered HTML contract — hidden inputs that make up the
POST payload, the required-repo guard, textarea content, and the combined
error list below the box. Client-side Alpine behavior (popover, autosize,
chip interactions) is verified manually in the browser, not here.
"""

from __future__ import annotations

from django.template.loader import render_to_string

from activity.forms import AgentRunCreateForm


def _render(form):
    return render_to_string("activity/_agent_run_fields.html", {"form": form})


def test_renders_hidden_repo_and_ref_inputs_from_bound_values():
    form = AgentRunCreateForm(initial={"prompt": "do the thing", "repo_id": "acme/api", "ref": "main", "use_max": True})
    html = _render(form)
    assert 'name="repo_id"' in html
    assert 'value="acme/api"' in html
    assert 'name="ref"' in html
    assert 'value="main"' in html


def test_renders_textarea_with_prompt_value():
    form = AgentRunCreateForm(initial={"prompt": "hello world", "repo_id": "x/y"})
    html = _render(form)
    assert 'name="prompt"' in html
    assert ">hello world</textarea>" in html


def test_renders_use_max_hidden_false_and_checkbox_checked_when_set():
    form = AgentRunCreateForm(initial={"prompt": "p", "repo_id": "x/y", "use_max": True})
    html = _render(form)
    assert '<input type="hidden" name="use_max" value="false"' in html
    assert 'name="use_max" value="true"' in html
    assert "checked" in html


def test_renders_use_max_checkbox_unchecked_when_not_set():
    form = AgentRunCreateForm(initial={"prompt": "p", "repo_id": "x/y", "use_max": False})
    html = _render(form)
    assert '<input type="hidden" name="use_max" value="false"' in html
    assert 'name="use_max" value="true"' in html
    assert " checked" not in html


def test_required_guard_has_value_when_repo_set():
    form = AgentRunCreateForm(initial={"prompt": "p", "repo_id": "x/y"})
    html = _render(form)
    assert 'name="__repo_required_guard"' in html
    assert 'value="ok"' in html


def test_required_guard_empty_when_repo_missing():
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert 'name="__repo_required_guard"' in html
    guard_idx = html.index('name="__repo_required_guard"')
    guard_fragment = html[guard_idx : guard_idx + 200]
    assert 'value="ok"' not in guard_fragment


def test_renders_combined_error_list_below_box():
    form = AgentRunCreateForm(data={"prompt": "", "repo_id": "", "ref": ""})
    form.is_valid()
    html = _render(form)
    assert 'class="mt-2 space-y-1 text-sm text-red-400"' in html
    assert html.count("<ul") >= 1
    assert "required" in html.lower()
