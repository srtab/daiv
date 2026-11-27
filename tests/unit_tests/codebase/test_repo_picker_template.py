from __future__ import annotations

from django.template.loader import render_to_string
from django.urls import reverse

from bs4 import BeautifulSoup

from codebase.models import RepositoryCatalog

CONTEXT = {"initial_repos": "[]", "max_repos": 1, "field_name": "", "required": False}


def test_default_variant_keeps_the_remove_control_and_choose_button():
    """The Run form shares this partial as a labeled multi-repo field: its chips must keep
    the ``×`` remove control and the neutral "Choose repository" button."""
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})

    assert "repo-chip__remove" in html
    assert "Choose repository" in html


def test_multi_repo_done_control_does_not_submit_the_enclosing_form():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})
    footer = BeautifulSoup(html, "html.parser").select_one(".repo-picker__footer")

    assert footer is not None
    done = footer.find("button", string="Done")
    assert done is not None
    assert done["type"] == "button"
    assert footer.find(attrs={"aria-live": "polite"}) is not None


def test_the_card_variant_renders_rows_with_make_main_and_remove():
    html = render_to_string(
        "codebase/_repo_picker.html", CONTEXT | {"variant": "card", "max_repos": 5, "unique_repos": True}
    )

    assert "uniqueRepos: true" in html
    assert "makeMain(index)" in html
    assert "remove(index)" in html
    assert "workspace-row" in html
    assert "repo-chip" not in html


def test_the_card_rows_name_their_controls_through_translated_templates():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"variant": "card", "max_repos": 5})
    rows = html[html.index('class="workspace-rows"') : html.index('class="workspace-add')]

    assert "' + repo.slug" not in rows
    assert rows.count(".replace('__LABEL__', repo.slug)") == 3


def test_unique_repos_is_off_by_default():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT)

    assert "uniqueRepos: false" in html


def test_filters_row_loads_facets_with_the_current_filters():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})
    soup = BeautifulSoup(html, "html.parser")
    filters = soup.select_one('[x-ref="repoFilters"]')

    assert filters["hx-get"] == reverse("codebase:picker-repository-facets")
    assert filters["hx-trigger"] == "refresh"
    assert "language" in filters[":hx-vals"]
    assert "topic" in filters[":hx-vals"]


def test_search_sends_filters_and_selection_with_every_request():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})
    search = BeautifulSoup(html, "html.parser").select_one('[x-ref="repoSearch"]')

    assert all(name in search[":hx-vals"] for name in ("language", "topic", "selected"))


def test_select_all_row_and_its_leftover_note_render():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})

    assert "selectAllMatching()" in html
    assert "selectAllNote" in html


def test_select_all_labels_reach_promptbox_in_both_pickers():
    picker = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})
    run_form = render_to_string("sessions/_agent_run_fields.html", {"form": None})

    for html in (picker, run_form):
        assert 'selectAllTemplate: "Select all __TOTAL__ matching"' in html
        assert "didntFitTemplate:" in html


def test_rows_announce_pressed_from_what_a_click_would_do():
    repo = RepositoryCatalog(slug="acme/api", name="acme/api", default_branch="main")
    html = render_to_string("codebase/_repo_picker_options.html", {"rows": [(repo, "")], "next_url": None})

    assert ":aria-pressed=\"isRepoPressed('acme/api', 'main').toString()\"" in html


def test_escape_goes_through_the_layered_handler():
    html = render_to_string("codebase/_repo_picker.html", CONTEXT | {"max_repos": 20})

    assert '@keydown.escape.window="escapeRepoPopover()"' in html
