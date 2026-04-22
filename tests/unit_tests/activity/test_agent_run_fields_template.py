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


def _tag(html, name):
    match = re.search(rf'<input[^>]*\bname="{re.escape(name)}"[^>]*>', html)
    assert match, f"no <input name={name!r}> in rendered HTML"
    return match.group(0)


def test_renders_hidden_repo_and_ref_inputs_from_bound_values():
    form = AgentRunCreateForm(initial={"prompt": "do the thing", "repo_id": "acme/api", "ref": "main", "use_max": True})
    html = _render(form)
    assert 'value="acme/api"' in _tag(html, "repo_id")
    assert 'value="main"' in _tag(html, "ref")


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
    assert 'value="ok"' in _tag(html, "__repo_required_guard")


def test_required_guard_empty_when_repo_missing():
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert 'value="ok"' not in _tag(html, "__repo_required_guard")


def test_renders_combined_error_list_below_box():
    form = AgentRunCreateForm(data={"prompt": "", "repo_id": "", "ref": ""})
    form.is_valid()
    html = _render(form)
    assert 'class="mt-2 space-y-1 text-sm text-red-400"' in html
    assert html.count("<ul") >= 1
    assert "required" in html.lower()


def test_hidden_inputs_escape_repo_id_and_ref():
    """Hostile values in ``repo_id`` / ``ref`` must not break attribute quoting or inject markup."""
    form = AgentRunCreateForm(initial={"prompt": "p", "repo_id": 'a"><script>x()</script>', "ref": 'v1 "q"'})
    html = _render(form)
    assert "<script>x()</script>" not in html
    # Django autoescape converts " to &quot; inside attribute values:
    assert "&quot;" in _tag(html, "repo_id")
    assert "&quot;" in _tag(html, "ref")


def test_empty_state_shows_choose_repository_button():
    """With no repo bound, the chip row renders the 'Choose repository' empty-state button."""
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert "Choose repository" in html


def test_repo_picker_popover_uses_picker_url():
    """The repo popover's search input uses the picker-repositories URL, not the old JSON endpoint."""
    form = AgentRunCreateForm(initial={"prompt": "p"})
    html = _render(form)
    assert reverse("codebase:picker-repositories") in html
    # The old _repo_combobox.html partial must no longer be included.
    assert "x-combobox" not in html


def test_branch_picker_template_references_branches_url_prefix():
    """The branch popover builds its hx-get URL in Alpine; the literal URL prefix must appear in the template."""
    form = AgentRunCreateForm(initial={"prompt": "p", "repo_id": "acme/api", "ref": "main"})
    html = _render(form)
    # The branch popover concatenates the slug at runtime; only the literal prefix is server-rendered.
    assert "/codebase/pickers/repositories/" in html
    assert "/branches/" in html
