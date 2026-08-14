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
    assert "picker-popover left-0" not in sheet
