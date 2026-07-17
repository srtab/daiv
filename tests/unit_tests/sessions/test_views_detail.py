from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.urls import reverse
from django.utils import timezone

import pytest
from sessions.locks import STALE_RUN_MINUTES
from sessions.models import Run, RunStatus, Session, SessionOrigin

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.CHAT, "repo_id": "group/project", "ref": "main"}
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.CHAT,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _null_hydration():
    """Patch target that returns an empty, non-expired hydration."""
    return AsyncMock(return_value=([], False, None))


def _hydration(messages):
    """Patch target that returns the given messages, non-expired."""
    return AsyncMock(return_value=(messages, False, None))


# ---------------------------------------------------------------------------
# session_new (chooser) + session_new_chat (empty state)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_session_new_requires_login(client):
    resp = client.get(reverse("session_new"))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_session_new_chat_requires_login(client):
    # The URL split moved the empty state to its own route; it must stay login-gated too.
    resp = client.get(reverse("session_new_chat"))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_session_new_chat_renders_empty_state(member_client):
    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_new_chat"))
    assert resp.status_code == 200
    assert resp.context["session"] is None
    assert resp.context["expired"] is False
    assert resp.context["turns"] == []


@pytest.mark.django_db
def test_session_new_renders_chooser_with_both_paths(member_client):
    resp = member_client.get(reverse("session_new"))
    assert resp.status_code == 200
    content = resp.content.decode()
    # The chooser links to both destinations — the guidance lives at the fork.
    assert reverse("session_new_chat") in content
    assert reverse("runs:agent_run_new") in content


# ---------------------------------------------------------------------------
# session_detail
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_detail_requires_login(client, member_user):
    session = _create_session(user=member_user)
    resp = client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_detail_404_for_other_users_session(member_client, other_user):
    session = _create_session(user=other_user)
    resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_detail_renders_for_own_session(member_client, member_user):
    session = _create_session(user=member_user)
    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert resp.status_code == 200
    assert resp.context["session"] == session
    assert resp.context["expired"] is False


@pytest.mark.django_db
def test_detail_with_live_checkpoint_renders_transcript(member_client, member_user):
    from langchain_core.messages import AIMessage

    session = _create_session(user=member_user)
    msg = AIMessage(content="hello from agent", id="m-1")
    tup = MagicMock(checkpoint={"channel_values": {"messages": [msg]}})

    with (
        patch("sessions.hydration.open_checkpointer") as cp_ctx,
        patch("sessions.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    turns = resp.context["turns"]
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert turns[0]["segments"] == [{"type": "text", "content": "hello from agent"}]


@pytest.mark.django_db
def test_detail_reconstructs_deltachannel_transcript(member_client, member_user):
    """Regression: deepagents' ``DeltaChannel`` leaves ``messages`` out of
    ``channel_values`` on non-snapshot steps, so a raw read yields an empty transcript on
    reload. The detail view must reconstruct it from the delta write history instead."""
    from langchain_core.messages import AIMessage, HumanMessage

    session = _create_session(user=member_user)
    # A live checkpoint whose channel_values has NO messages key (DeltaChannel non-snapshot).
    tup = MagicMock(checkpoint={"channel_values": {"session_id": "x"}})
    writes = [
        ("t0", "messages", [HumanMessage(content="count todos", id="h1")]),
        ("t1", "messages", [AIMessage(content="there are 18", id="a1")]),
    ]

    with (
        patch("sessions.hydration.open_checkpointer") as cp_ctx,
        patch("sessions.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        saver.aget_delta_channel_history = AsyncMock(return_value={"messages": {"writes": writes}})
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    turns = resp.context["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["segments"] == [{"type": "text", "content": "count todos"}]
    assert turns[1]["segments"] == [{"type": "text", "content": "there are 18"}]


@pytest.mark.django_db
def test_detail_with_missing_checkpoint_flags_expired(member_client, member_user):
    session = _create_session(user=member_user)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True


@pytest.mark.django_db
def test_detail_expired_checkpoint_disables_composer(member_client, member_user):
    """_ahydrate returning (.., expired=True, ..) => context['expired'] is True
    and the template renders the expired notice."""
    session = _create_session(user=member_user)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)  # tup=None => expired
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True
    # Template should contain the expired notice text
    content = resp.content.decode()
    assert "expired" in content.lower() or "state has expired" in content.lower()


@pytest.mark.django_db
def test_detail_missing_checkpoint_not_expired_while_run_in_flight(member_client, member_user):
    """A just-submitted background run has no checkpoint yet; that must NOT render as
    'expired'. Instead the in-flight working state + transcript polling take over."""
    session = _create_session(user=member_user, ref="")  # ref="" skips the MR-payload lookup
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.RUNNING)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)  # no checkpoint yet
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert resp.context["is_in_flight"] is True
    content = resp.content.decode()
    # In-flight + no checkpoint renders the working state, not the expired banner.
    assert "Agent is working" in content
    assert "has expired" not in content


@pytest.mark.django_db
def test_detail_missing_checkpoint_expired_when_all_runs_terminal(member_client, member_user):
    """No checkpoint AND no in-flight run => genuinely expired; banner still shows."""
    session = _create_session(user=member_user, ref="")
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.SUCCESSFUL)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True


@pytest.mark.django_db
def test_detail_missing_checkpoint_replays_prompt_turn_when_last_run_failed(member_client, member_user):
    """A SCHEDULE run that failed before checkpointing: no transcript, but its prompt survives.
    annotate_transcript replays the prompt as a user turn and appends a run_status marker.
    The raw traceback (non-chat run) must never reach the page (§F safety guarantee)."""
    from core.constants import RUN_FAILED_MESSAGE

    session = _create_session(user=member_user, ref="")
    _create_run(
        session,
        trigger_type=SessionOrigin.SCHEDULE,
        status=RunStatus.FAILED,
        prompt="Triage this week's Sentry errors and open MRs.",
        error_message="git.exc.GitCommandError: clone failed",
    )

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert "failed_run" not in resp.context

    turns = resp.context["turns"]
    # The prompt is replayed as a user turn (no errored key in new design).
    prompt_turn = next(t for t in turns if t["role"] == "user")
    assert prompt_turn["segments"][0]["content"] == "Triage this week's Sentry errors and open MRs."
    assert "errored" not in prompt_turn

    # A run_status marker is appended after the user turn.
    marker = next(t for t in turns if t["role"] == "run_status")
    assert marker["status"] == "failed"
    # §F: non-chat run → generic message, raw traceback must not leak.
    assert marker["message"] == RUN_FAILED_MESSAGE

    content = resp.content.decode()
    # §F: raw traceback must never reach the page.
    assert "git.exc.GitCommandError" not in content
    # Composer stays available to retry in place.
    assert "chat-composer" in content


@pytest.mark.django_db
def test_detail_failed_run_without_prompt_shows_marker(member_client, member_user):
    """An ISSUE_WEBHOOK run that failed with no prompt: a run_status marker appears (no user
    turn, since no prompt). The raw error text (non-chat) must never reach the page (§F)."""
    from core.constants import RUN_FAILED_MESSAGE

    session = _create_session(user=member_user, ref="")
    _create_run(
        session,
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        status=RunStatus.FAILED,
        prompt="",
        error_message="RuntimeError: boom",
    )

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    assert "failed_run" not in resp.context

    turns = resp.context["turns"]
    # No prompt → no user turn; only the run_status marker.
    assert len(turns) == 1
    assert turns[0]["role"] == "run_status"
    assert turns[0]["status"] == "failed"
    assert turns[0]["message"] == RUN_FAILED_MESSAGE

    content = resp.content.decode()
    # §F: raw traceback must never reach the page.
    assert "RuntimeError: boom" not in content


@pytest.mark.django_db
def test_detail_uses_run_status_chip_not_per_turn_error_markup(member_client, member_user):
    """Failures now render as a standalone run-status chip, not per-turn error markup.
    The chip <template> is present regardless of data; the old per-turn error markup is gone."""
    session = _create_session(user=member_user)
    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    content = resp.content.decode()
    assert "chat-run-status" in content  # chip template present (Alpine <template>)
    assert "chat-turn__error" not in content  # old per-turn error markup removed
    assert "chat-turn--errored" not in content
    assert 'x-text="turn.error"' not in content  # no raw-error binding


@pytest.mark.django_db
def test_detail_missing_checkpoint_expired_when_last_run_succeeded(member_client, member_user):
    """A missing checkpoint whose runs all SUCCEEDED is a genuine TTL expiry — annotate_transcript
    recovers no turns (only FAILED runs synthesize a marker), so the expired banner shows."""
    session = _create_session(user=member_user, ref="")
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.SUCCESSFUL)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True
    assert "has expired" in resp.content.decode()


@pytest.mark.django_db
def test_detail_missing_checkpoint_expired_when_run_stale(member_client, member_user):
    """A non-terminal run whose holder stopped heartbeating (crashed worker) past
    STALE_RUN_MINUTES is dead — it must fall through to 'expired', not pin the view
    on a permanent 'working' spinner. Same setup as the in-flight test above, but a
    stale ``last_active_at`` flips the outcome."""
    session = _create_session(
        user=member_user, ref="", last_active_at=timezone.now() - timedelta(minutes=STALE_RUN_MINUTES + 1)
    )
    _create_run(session, trigger_type=SessionOrigin.UI_JOB, status=RunStatus.RUNNING)

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)  # no checkpoint
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    # Stale heartbeat => not in flight => expired banner, no working spinner.
    assert resp.context["is_in_flight"] is False
    assert resp.context["expired"] is True
    assert "Agent is working" not in resp.content.decode()


@pytest.mark.django_db
def test_detail_visible_to_run_actor(member_client, member_user):
    """A webhook session (user=None) is reachable by the external actor via by_owner."""
    session = _create_session(user=None, external_username=member_user.username, origin=SessionOrigin.ISSUE_WEBHOOK)

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["session"] == session


@pytest.mark.django_db
def test_detail_in_flight_context(member_client, member_user):
    """is_in_flight and in_flight_ids are populated from non-terminal runs."""
    session = _create_session(user=member_user)
    run_done = _create_run(session, status=RunStatus.SUCCESSFUL)
    run_live = _create_run(session, status=RunStatus.RUNNING)

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.context["is_in_flight"] is True
    assert str(run_live.id) in resp.context["in_flight_ids"]
    assert str(run_done.id) not in resp.context["in_flight_ids"]


@pytest.mark.django_db
def test_poll_transcript_only_for_background_runs(member_client, member_user):
    """poll_transcript is True only for non-chat in-flight runs; chat runs manage themselves via AG-UI stream."""
    # Case 1: in-flight CHAT run — poller must NOT engage (chat uses AG-UI stream).
    session_chat = _create_session(user=member_user)
    chat_run = _create_run(session_chat, trigger_type=SessionOrigin.CHAT, status=RunStatus.RUNNING)
    session_chat.active_run_id = chat_run.id
    session_chat.save(update_fields=["active_run_id"])

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_chat.thread_id}))

    assert resp.context["poll_transcript"] is False, (
        "A live CHAT run should not activate the transcript poller (it streams via AG-UI)"
    )

    # Case 2: in-flight background (API_JOB) run — poller MUST engage.
    session_bg = _create_session(user=member_user)
    bg_run = _create_run(session_bg, trigger_type=SessionOrigin.API_JOB, status=RunStatus.RUNNING)
    session_bg.active_run_id = bg_run.id
    session_bg.save(update_fields=["active_run_id"])

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_bg.thread_id}))

    assert resp.context["poll_transcript"] is True, (
        "A live background (API_JOB) run should activate the transcript poller"
    )

    # Case 3: all runs are terminal — poller must NOT engage.
    session_done = _create_session(user=member_user)
    done_run = _create_run(session_done, trigger_type=SessionOrigin.API_JOB, status=RunStatus.SUCCESSFUL)
    session_done.active_run_id = done_run.id
    session_done.save(update_fields=["active_run_id"])

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_done.thread_id}))

    assert resp.context["poll_transcript"] is False, "All-terminal runs should not activate the transcript poller"


@pytest.mark.django_db
def test_chat_active_run_id_only_for_chat_holders(member_client, member_user):
    """The chat page rejoins the event relay only for chat-origin holders; a
    background (API_JOB) holder must yield an empty chat_active_run_id so the JS
    leaves live updates to the transcript poller instead of tailing a relay
    stream that nothing publishes to."""
    # Case 1: in-flight CHAT run — the AG-UI holder id must be exposed for resume.
    session_chat = _create_session(user=member_user)
    _create_run(session_chat, trigger_type=SessionOrigin.CHAT, status=RunStatus.RUNNING)
    session_chat.active_run_id = "agui-run-1"
    session_chat.save(update_fields=["active_run_id"])

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_chat.thread_id}))

    assert resp.context["chat_active_run_id"] == "agui-run-1"

    # Case 2: in-flight background run — must be empty.
    session_bg = _create_session(user=member_user)
    bg_run = _create_run(session_bg, trigger_type=SessionOrigin.API_JOB, status=RunStatus.RUNNING)
    session_bg.active_run_id = bg_run.id
    session_bg.save(update_fields=["active_run_id"])

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_bg.thread_id}))

    assert resp.context["chat_active_run_id"] == ""

    # Case 3: all runs terminal — must be empty (nothing to resume).
    session_done = _create_session(user=member_user)
    done_run = _create_run(session_done, trigger_type=SessionOrigin.CHAT, status=RunStatus.SUCCESSFUL)
    session_done.active_run_id = done_run.id
    session_done.save(update_fields=["active_run_id"])

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session_done.thread_id}))

    assert resp.context["chat_active_run_id"] == ""


@pytest.mark.django_db
def test_failed_middle_run_gets_marker_after_its_turn(member_client, member_user):
    """A CHAT run that FAILED mid-session: its run_status marker must sit immediately
    after the last assistant turn belonging to that run, before the next user turn."""
    from langchain_core.messages import AIMessage, HumanMessage

    session = _create_session(user=member_user, ref="")
    _create_run(session, trigger_type=SessionOrigin.CHAT, status=RunStatus.FAILED, message_id="h1")
    _create_run(session, trigger_type=SessionOrigin.CHAT, status=RunStatus.SUCCESSFUL, message_id="h2")

    messages = [
        HumanMessage(content="one", id="h1"),
        AIMessage(content="partial", id="a1"),
        HumanMessage(content="two", id="h2"),
        AIMessage(content="done", id="a2"),
    ]
    with patch("sessions.views.ahydrate_thread", _hydration(messages)):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    turns = resp.context["turns"]
    roles = [t["role"] for t in turns]
    assert "run_status" in roles
    marker_idx = roles.index("run_status")
    assert turns[marker_idx - 1]["id"] == "a1"  # marker follows the failed run's assistant turn
    assert turns[marker_idx + 1]["id"] == "h2"


@pytest.mark.django_db
def test_detail_expired_checkpoint_recovers_every_failed_run(member_client, member_user):
    """A lapsed checkpoint (no messages) with several past FAILED runs recovers each run's
    prompt + marker instead of showing a bare expired banner — the banner is suppressed
    precisely because annotate_transcript produced turns to render."""
    session = _create_session(user=member_user, ref="")
    _create_run(
        session,
        trigger_type=SessionOrigin.CHAT,
        status=RunStatus.FAILED,
        error_message="Run failed. Check server logs for details.",
        prompt="first ask",
    )
    _create_run(
        session,
        trigger_type=SessionOrigin.CHAT,
        status=RunStatus.FAILED,
        error_message="Run failed. Check server logs for details.",
        prompt="second ask",
    )

    with patch("sessions.hydration.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False  # turns recovered → not a bare expiry
    roles = [t["role"] for t in resp.context["turns"]]
    assert roles.count("run_status") == 2
    assert "has expired" not in resp.content.decode()


# ---------------------------------------------------------------------------
# session_run_download_md
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_download_md_serves_run_result(member_client, member_user):
    """GET session_run_download_md for a SUCCESSFUL run returns markdown attachment."""
    session = _create_session(user=member_user)
    run = _create_run(session, status=RunStatus.SUCCESSFUL, result_summary="# Hello\n\nWorld", user=member_user)

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))

    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/markdown")
    assert "attachment" in resp["Content-Disposition"]
    content = b"".join(resp.streaming_content) if hasattr(resp, "streaming_content") else resp.content
    text = content.decode()
    assert "Hello" in text


@pytest.mark.django_db
def test_download_md_404_for_non_successful_run(member_client, member_user):
    """Failed runs cannot be downloaded."""
    session = _create_session(user=member_user)
    run = _create_run(session, status=RunStatus.FAILED, result_summary="some error", user=member_user)

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_download_md_404_for_other_users_run(member_client, other_user):
    """Other user's run cannot be downloaded."""
    session = _create_session(user=other_user)
    run = _create_run(session, status=RunStatus.SUCCESSFUL, result_summary="some result")

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_download_md_404_when_no_result_summary(member_client, member_user):
    """Runs with empty result_summary return 404 — nothing to serve."""
    session = _create_session(user=member_user)
    run = _create_run(session, status=RunStatus.SUCCESSFUL, result_summary="", user=member_user)

    resp = member_client.get(reverse("session_run_download_md", kwargs={"thread_id": session.thread_id, "pk": run.id}))
    assert resp.status_code == 404
