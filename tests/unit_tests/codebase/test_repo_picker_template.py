from __future__ import annotations

from django.template.loader import render_to_string

from bs4 import BeautifulSoup

CONTEXT = {"initial_repos": "[]", "max_repos": 1, "field_name": "", "required": False}


def test_default_variant_keeps_the_remove_control_and_choose_button():
    """The Run form shares this partial as a labeled multi-repo field: its chips must keep
    the ``×`` remove control and the neutral "Choose repository" button."""
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})

    assert "repo-chip__remove" in html
    assert "Choose repository" in html
    assert "repo-context-trigger" not in html
    assert "repo-chip__caret" not in html


def test_context_variant_swaps_remove_for_a_chevron_and_accents_the_empty_state():
    """The composer's context row always carries exactly one repo, so removal is
    meaningless there — the chevron replaces it and marks the chip as changeable. Accent
    is reserved for the empty state, the one moment the row is an instruction."""
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"variant": "context"})

    assert "repo-context-trigger" in html
    assert "Pick a repository" in html
    assert "repo-chip__caret" in html
    assert "repo-chip__remove" not in html


def test_multi_repo_done_control_does_not_submit_the_enclosing_form():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})
    footer = BeautifulSoup(html, "html.parser").select_one(".repo-picker__footer")

    assert footer is not None
    done = footer.find("button", string="Done")
    assert done is not None
    assert done["type"] == "button"
    assert footer.find(attrs={"aria-live": "polite"}) is not None
