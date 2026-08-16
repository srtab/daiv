"""Markup-level guards for the redesigned chat composer.

These assert the *anatomy* the composer is built around — one context row in the dock,
one action row, everything else behind a sheet — and that the surfaces it replaced are
really gone. They deliberately don't assert on the Alpine-only branches (both the locked
and the live model label ship in the HTML; which one shows is a client-side decision).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from django.urls import reverse

import pytest
from sessions.models import Session, SessionOrigin

from tests.unit_tests.test_picker_popovers import CONTAINER as POPOVER_CONTAINER
from tests.unit_tests.test_picker_popovers import INPUT_CSS
from tests.unit_tests.test_template_comments import DAIV_DIR


def _create_session(user, **kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.CHAT,
        "repo_id": "group/project",
        "ref": "main",
        "user": user,
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _render_new_chat(client) -> str:
    with patch("sessions.views.ahydrate_thread", AsyncMock(return_value=([], False, None, None))):
        return client.get(reverse("session_new_chat")).content.decode()


def _render_thread(client, session: Session) -> str:
    with (
        patch("sessions.views.ahydrate_thread", AsyncMock(return_value=([], False, None, None))),
        patch("sessions.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        return client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id})).content.decode()


@pytest.mark.django_db
def test_summary_strip_and_rail_are_gone(member_client, member_user):
    """Both duplicated what the composer now carries: the strip repeated repo/branch, the
    rail repeated todos and files. One anatomy at every width means neither survives."""
    hero = _render_new_chat(member_client)
    thread = _render_thread(member_client, _create_session(member_user))

    for html in (hero, thread):
        assert "chat-summary" not in html
        assert "chat-rail" not in html


@pytest.mark.django_db
def test_context_row_lives_in_the_dock_above_the_composer(member_client, member_user):
    """Repo and branch belong to the sticky dock, not the scrolling column — the two facts
    people misjudge have to stay on screen."""
    html = _render_thread(member_client, _create_session(member_user))

    dock = html.index('class="chat-dock"')
    context_row = html.index('class="chat-context"')
    composer = html.index('class="chat-composer"')
    assert dock < context_row < composer


@pytest.mark.django_db
def test_new_chat_context_row_is_the_repo_picker(member_client):
    """State 01: nothing picked yet, so the row is an accent instruction rather than a fact."""
    html = _render_new_chat(member_client)

    assert "repo-context-trigger" in html
    assert "Pick a repository" in html


@pytest.mark.django_db
def test_send_is_icon_only(member_client, member_user):
    """Icon-only Send is what buys the width the model label needs; the old labelled
    button class must not survive alongside it."""
    html = _render_thread(member_client, _create_session(member_user))

    assert "composer-send" in html
    assert "icons/arrow-up.svg" in html
    assert "chat-composer__btn" not in html


@pytest.mark.django_db
def test_composer_rests_at_one_row(member_client, member_user):
    """A fixed second row put the action row a dead line below the text; the box's height
    is its content now (`field-sizing` in input.css, `autosize()` where that's missing)."""
    html = _render_thread(member_client, _create_session(member_user))

    assert 'rows="1"' in html


@pytest.mark.django_db
def test_both_sheets_are_rendered_with_their_triggers(member_client, member_user):
    html = _render_thread(member_client, _create_session(member_user))

    assert "composer-sheet--options" in html
    assert "composer-sheet--progress" in html
    assert "composer-trigger--icon" in html
    assert "composer-trigger--pill" in html


@pytest.mark.django_db
def test_model_label_is_locked_for_an_existing_thread(member_client, member_user):
    """The API rejects a changed ``agent_model`` after the first turn, so the label has to
    read as pinned. The live picker is ``x-if``'d out for the same reason: mounted, it
    would seed a hidden input with the site default and get the next turn 409'd."""
    session = _create_session(member_user, agent_model="openrouter:z-ai/glm-5.2", agent_thinking_level="high")
    html = _render_thread(member_client, session)

    assert "model-label--locked" in html
    assert "Locked for this conversation" in html
    assert 'x-if="!thread"' in html


@pytest.mark.django_db
def test_hero_carries_a_selection_line_instead_of_pickers(member_client):
    """The three hero pickers are replaced by the context row plus one quiet line that
    opens the options sheet."""
    html = _render_new_chat(member_client)

    assert "chat-hero__selection" in html
    assert "chat-hero__picker" not in html


@pytest.mark.django_db
def test_progress_sheet_tints_the_line_counts(member_client, member_user):
    """``+x`` and ``−y`` are tinted wherever they appear — on the pill and on the Files
    changed group label — rather than rendered as one flat string."""
    html = _render_thread(member_client, _create_session(member_user))

    assert "diff-stat__plus" in html
    assert "diff-stat__minus" in html


@pytest.mark.django_db
def test_effort_word_yields_only_when_the_row_is_crowded(member_client, member_user):
    """The model label sheds its effort word before the name truncates, but only once a
    progress pill is actually sharing the action row — a phone with no pill keeps it."""
    html = _render_thread(member_client, _create_session(member_user))

    assert "chat-composer__actions--crowded" in html
    assert "'chat-composer__actions--crowded': progressPill" in html


@pytest.mark.django_db
def test_options_sheet_hosts_no_floating_popover(member_client, member_user):
    """Environment and Tools share one interaction model, and neither escapes the sheet:
    a bottom sheet has nothing below it for a popover to open into."""
    html = _render_thread(member_client, _create_session(member_user))

    sheet_start = html.index('class="composer-sheet composer-sheet--options')
    sheet = html[sheet_start : html.index("composer-sheet composer-sheet--progress")]
    assert sheet.count("sheet-disclosure") >= 2
    assert not POPOVER_CONTAINER.search(sheet)


COMMAND_MENU = DAIV_DIR / "chat" / "templates" / "chat" / "_command_menu.html"
CHAT_STREAM_JS = DAIV_DIR / "chat" / "static" / "chat" / "js" / "chat-stream.js"


@pytest.mark.django_db
def test_composer_renders_the_command_autocomplete(member_client, member_user):
    """The "/" menu ships on both the empty hero state and an existing thread, seeded by
    the ``chat-slash-commands`` json_script; ``@mousedown.prevent`` is what keeps the
    textarea focused through a row click."""
    hero = _render_new_chat(member_client)
    thread = _render_thread(member_client, _create_session(member_user))

    for html in (hero, thread):
        assert "composer-autocomplete" in html
        assert "chat-slash-commands" in html
        assert "slashMenuOpen" in html
        assert "@mousedown.prevent" in html


def test_autocomplete_is_not_a_picker_popover_or_sheet():
    """The menu spans the composer's own width, so the phone-overflow problem that forces
    trigger-anchored pickers into bottom sheets never applies — and a bottom sheet would
    cover the textarea being typed in. This is also why EXPECTED_SURFACES stays put."""
    source = COMMAND_MENU.read_text(encoding="utf-8")

    assert not POPOVER_CONTAINER.search(source)
    assert 'class="composer-sheet ' not in source
    assert "x-transition" not in source


def test_autocomplete_is_on_the_surface_rise_roster():
    """A surface that appears without motion reads as a repaint glitch next to the ones
    that rise; membership is the one grouped rule at the top of input.css."""
    css = INPUT_CSS.read_text(encoding="utf-8")

    roster_start = css.index(".surface-rise,")
    roster = css[roster_start : css.index("{", roster_start)]
    assert ".composer-autocomplete" in roster


@pytest.mark.django_db
def test_autocomplete_rows_are_options_the_textarea_drives(member_client):
    """Enter rewrites the field, so the field has to say what it is bound to. Rows are
    options rather than tab stops — Shift+Tab must not land inside the menu."""
    html = _render_new_chat(member_client)

    assert 'role="listbox"' in html
    assert 'role="option"' in html
    assert 'aria-controls="chat-command-menu"' in html
    assert ':aria-activedescendant="slashMenuOpen ?' in html
    assert 'tabindex="-1"' in html


def test_autocomplete_never_steals_a_key_it_was_not_offered():
    """Two ways the menu could eat a keystroke meant for the draft: an IME committing a
    candidate with Enter, and a re-armed menu after the user dismissed it."""
    js = CHAT_STREAM_JS.read_text(encoding="utf-8")

    assert "e.isComposing" in js
    assert "if (this.slashToken === null) this.slashDismissed = false;" in js


def test_autocomplete_kind_badge_avoids_a_js_string_literal():
    """A translation carrying an apostrophe would close the literal an ``x-text`` ternary
    compiles, so the two words render as sibling spans instead."""
    source = COMMAND_MENU.read_text(encoding="utf-8")

    assert "? '{% translate" not in source


def test_autocomplete_announces_via_its_own_surface_group_slot():
    """One slot per surface: the menu must not share the sheets' close callback, or
    opening it would not dismiss an open sheet (and vice versa)."""
    js = CHAT_STREAM_JS.read_text(encoding="utf-8")

    assert js.count("surfaceGroup.join(") == 2
    assert "_announceSlashOpen" in js
