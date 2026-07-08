from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from django.urls import reverse

import pytest
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


# ---------------------------------------------------------------------------
# session_new (empty state)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_session_new_requires_login(client):
    resp = client.get(reverse("session_new"))
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_session_new_renders_empty_state(member_client):
    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_new"))
    assert resp.status_code == 200
    assert resp.context["session"] is None
    assert resp.context["expired"] is False
    assert resp.context["turns"] == []


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
def test_detail_includes_run_timeline(member_client, member_user):
    """A session with two runs renders both in the timeline rail with status pills."""
    session = _create_session(user=member_user)
    run1 = _create_run(session, status=RunStatus.SUCCESSFUL)
    run2 = _create_run(session, status=RunStatus.FAILED)

    with patch("sessions.views.ahydrate_thread", _null_hydration()):
        resp = member_client.get(reverse("session_detail", kwargs={"thread_id": session.thread_id}))

    assert resp.status_code == 200
    runs = resp.context["runs"]
    assert len(runs) == 2
    run_ids = {r.id for r in runs}
    assert run1.id in run_ids
    assert run2.id in run_ids
    # Both status pills should be visible in the rendered HTML
    content = resp.content.decode()
    assert f"run-{run1.id}" in content
    assert f"run-{run2.id}" in content


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
