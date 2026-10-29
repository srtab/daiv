"""Tests for sessions.api.views — the session_turns endpoint."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from ninja.testing import TestAsyncClient
from sessions.hydration import HydratedThread
from sessions.models import HiddenSession, Run, RunStatus, Session, SessionOrigin

from accounts.models import APIKey, User
from daiv.api import api
from tests.unit_tests.sessions.conftest import grant_read_access, make_artifact


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
async def authed(db):
    """Return (APIKey, raw_key, user) for authenticated tests."""
    user = await User.objects.acreate_user(
        username="sessuser",
        email="sessuser@example.com",
        password="testpass123",  # noqa: S106
    )
    key_obj, raw = await APIKey.objects.create_key(user=user, name="Test")
    return key_obj, raw, user


def _auth_headers(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


def _create_session(user=None, **kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.CHAT, "repo_id": "group/project", "ref": "main"}
    if user is not None:
        defaults["user"] = user
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


# ---------------------------------------------------------------------------
# auth gate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_session_turns_rejects_anonymous_and_bad_token(db):
    """``session_turns`` sits behind AuthBearer/django_auth — anonymous or bad-token callers
    get 401 and never reach the session lookup (no transcript leak). Exercised through the
    real Django client so the SessionMiddleware ``django_auth`` needs is present (the ninja
    TestAsyncClient's mock request has no session)."""
    from django.test import Client
    from django.urls import reverse

    anon = Client()
    url = reverse("api:session_turns", kwargs={"thread_id": str(uuid.uuid4())})
    assert anon.get(url).status_code == 401, "anonymous should be 401"
    assert anon.get(url, HTTP_AUTHORIZATION="Bearer not-a-real-key").status_code == 401, "bad token should be 401"


# ---------------------------------------------------------------------------
# session_turns
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_session_turns_returns_built_turns(client, authed):
    """Patch ahydrate_thread to return two fake messages; response contains build_turns
    output and expired=False."""
    from langchain_core.messages import AIMessage, HumanMessage

    _key_obj, raw, user = authed

    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id=None,
    )

    fake_messages = [HumanMessage(content="hello", id="m-1"), AIMessage(content="world", id="m-2")]

    with patch(
        "sessions.api.views.ahydrate_thread",
        AsyncMock(return_value=HydratedThread(fake_messages, False, None, None, None)),
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200
    data = resp.json()
    assert data["expired"] is False
    assert data["active"] is False
    # build_turns should produce one user turn and one assistant turn
    roles = [t["role"] for t in data["turns"]]
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.django_db(transaction=True)
async def test_session_turns_includes_run_status_marker_for_failed_run(client, authed):
    """The JSON poller endpoint runs annotate_transcript, not bare build_turns: a FAILED chat
    run whose message_id matches a hydrated human turn yields a run_status marker in the payload."""
    from langchain_core.messages import AIMessage, HumanMessage

    _key_obj, raw, user = authed

    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id=None,
    )
    await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.CHAT,
        repo_id="group/project",
        status=RunStatus.FAILED,
        message_id="m-1",
        error_message="Run failed. Check server logs for details.",
    )

    fake_messages = [HumanMessage(content="hello", id="m-1"), AIMessage(content="world", id="m-2")]

    with patch(
        "sessions.api.views.ahydrate_thread",
        AsyncMock(return_value=HydratedThread(fake_messages, False, None, None, None)),
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200
    markers = [t for t in resp.json()["turns"] if t["role"] == "run_status"]
    assert len(markers) == 1
    assert markers[0]["status"] == "failed"
    assert markers[0]["message"] == "Run failed. Check server logs for details."


@pytest.mark.django_db(transaction=True)
async def test_session_turns_expired(client, authed):
    """ahydrate_thread returns (.., True, ..) -> {"turns": [], "expired": true, "active": false}."""
    _key_obj, raw, user = authed

    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id=None,
    )

    with patch(
        "sessions.api.views.ahydrate_thread", AsyncMock(return_value=HydratedThread([], True, None, None, None))
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200
    data = resp.json()
    assert data["expired"] is True
    assert data["turns"] == []
    assert data["active"] is False


@pytest.mark.django_db(transaction=True)
async def test_session_turns_expired_still_returns_the_sessions_artifacts(client, authed):
    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="main", user=user
    )
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.UI_JOB, repo_id="group/project", status=RunStatus.SUCCESSFUL
    )
    artifact = await sync_to_async(make_artifact)(run)

    with patch(
        "sessions.api.views.ahydrate_thread", AsyncMock(return_value=HydratedThread([], True, None, None, None))
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200
    data = resp.json()
    assert data["expired"] is True
    assert [t["id"] for t in data["turns"]] == [f"run-{run.id}-artifacts"]
    assert [s["id"] for s in data["turns"][0]["segments"]] == [f"artifact-{artifact.id}"]


@pytest.mark.django_db(transaction=True)
async def test_session_turns_with_a_checkpoint_adds_no_artifact_turns(client, authed):
    from langchain_core.messages import HumanMessage

    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="main", user=user
    )
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.UI_JOB, repo_id="group/project", status=RunStatus.SUCCESSFUL
    )
    await sync_to_async(make_artifact)(run)
    hydrated = HydratedThread([HumanMessage(content="hi", id="h1")], False, None, None, None)

    with patch("sessions.api.views.ahydrate_thread", AsyncMock(return_value=hydrated)):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200
    assert not any(t["id"].endswith("-artifacts") for t in resp.json()["turns"])


@pytest.mark.django_db(transaction=True)
async def test_session_turns_404_for_other_users_session(client, authed):
    """turns goes through the same by_owner scoping as status — a stranger's thread_id
    must 404 (no re-hydrated transcript leak), and ahydrate_thread is never reached."""
    _key_obj, raw, _user = authed
    other = await User.objects.acreate_user(
        username="other-turns",
        email="other-turns@example.com",
        password="x",  # noqa: S106
    )
    session_other = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project", ref="main", user=other
    )

    hydrate = AsyncMock(return_value=HydratedThread([], False, None, None, None))
    with patch("sessions.api.views.ahydrate_thread", hydrate):
        resp = await client.get(f"/sessions/{session_other.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 404
    hydrate.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_session_turns_serves_a_reader_of_the_sessions_repository(client, authed):
    """The detail page shows a session on a repo the caller can read, so its poller must not 404 on it."""
    _key_obj, raw, user = authed
    owner = await User.objects.acreate_user(username="owner-turns", email="owner-turns@example.com", password="x")  # noqa: S106
    await sync_to_async(grant_read_access)(user, "group/project")
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="main", user=owner
    )

    with patch(
        "sessions.api.views.ahydrate_thread", AsyncMock(return_value=HydratedThread([], False, None, None, None))
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200


@pytest.mark.django_db(transaction=True)
async def test_session_turns_expired_keeps_a_failed_runs_turns(client, authed):
    """An expired checkpoint still shows each failed run's prompt and marker, as the detail page does."""
    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="main", user=user
    )
    run = await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.UI_JOB,
        repo_id="group/project",
        status=RunStatus.FAILED,
        prompt="fix the build",
        error_message="Run failed.",
    )

    with patch(
        "sessions.api.views.ahydrate_thread", AsyncMock(return_value=HydratedThread([], True, None, None, None))
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    turns = resp.json()["turns"]
    assert [(t["id"], t["role"]) for t in turns][0] == (f"run-{run.id}", "user")
    assert turns[1]["role"] == "run_status"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("heartbeat_age", "active"), [(timedelta(0), True), (timedelta(minutes=31), False)])
async def test_session_turns_active_needs_a_fresh_heartbeat(client, authed, heartbeat_age, active):
    """A holder past the staleness window is dead: the poller must stop, as the page no longer counts it in flight."""
    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.UI_JOB,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id="holder-1",
        last_active_at=timezone.now() - heartbeat_age,
    )
    await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.UI_JOB, repo_id="group/project", status=RunStatus.RUNNING
    )

    with patch(
        "sessions.api.views.ahydrate_thread", AsyncMock(return_value=HydratedThread([], False, None, None, None))
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.json()["active"] is active


@pytest.mark.django_db(transaction=True)
async def test_session_turns_names_the_live_run(client, authed):
    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="main", user=user
    )
    run = await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.UI_JOB,
        repo_id="group/project",
        status=RunStatus.RUNNING,
        started_at=timezone.now(),
    )
    hydrated = AsyncMock(return_value=HydratedThread([], False, None, None, None))

    with patch("sessions.api.views.ahydrate_thread", hydrated):
        idle = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))
        await Session.objects.filter(pk=session.pk).aupdate(active_run_id="42", last_active_at=timezone.now())
        live = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert idle.json()["live_run"] is None
    assert live.json()["live_run"] == {"id": "42", "stoppable": False, "started_at": run.started_at.isoformat()}


@pytest.mark.django_db(transaction=True)
async def test_session_turns_waits_until_the_run_runs(client, authed):
    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="main", user=user
    )
    run = await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.UI_JOB,
        repo_id="group/project",
        status=RunStatus.READY,
        prompt="please fix X",
    )
    hydrated = AsyncMock(return_value=HydratedThread([], False, None, None, None))

    with patch("sessions.api.views.ahydrate_thread", hydrated):
        ready = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.RUNNING)
        unclaimed = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))
        await Session.objects.filter(pk=session.pk).aupdate(active_run_id=str(run.pk))
        running = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert ready.json()["waiting_since"] == run.created_at.isoformat()
    assert [t.get("pending") for t in ready.json()["turns"]] == [True]
    assert unclaimed.json()["waiting_since"] == run.created_at.isoformat()
    assert running.json()["waiting_since"] is None
    assert [(t["segments"][0]["content"], t.get("pending")) for t in running.json()["turns"]] == [
        ("please fix X", None)
    ]


@pytest.mark.django_db(transaction=True)
async def test_session_turns_offers_stop_only_to_the_sessions_owner(client, authed):
    _key_obj, raw, owner = authed
    reader = await User.objects.acreate_user(username="reader-turns", email="reader-turns@example.com", password="x")  # noqa: S106
    _reader_key, reader_raw = await APIKey.objects.create_key(user=reader, name="Reader")
    await sync_to_async(grant_read_access)(reader, "group/project")
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=owner,
        active_run_id="agui-1",
    )
    await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.CHAT, repo_id="group/project", status=RunStatus.RUNNING
    )
    hydrated = AsyncMock(return_value=HydratedThread([], False, None, None, None))

    with patch("sessions.api.views.ahydrate_thread", hydrated):
        as_reader = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(reader_raw))
        as_owner = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert as_reader.json()["live_run"]["stoppable"] is False
    assert as_owner.json()["live_run"]["stoppable"] is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("trigger_type", "background"), [(SessionOrigin.DELEGATION_RESUME, True), (SessionOrigin.CHAT, False)]
)
async def test_session_turns_says_whether_background_work_is_in_flight(client, authed, trigger_type, background):
    """The detail page arms its transcript poller from this flag when a run starts after it rendered."""
    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id="holder-1",
        last_active_at=timezone.now(),
    )
    await Run.objects.acreate(
        session=session, trigger_type=trigger_type, repo_id="group/project", status=RunStatus.RUNNING
    )

    with patch(
        "sessions.api.views.ahydrate_thread", AsyncMock(return_value=HydratedThread([], False, None, None, None))
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.json()["background"] is background


@pytest.mark.django_db(transaction=True)
async def test_session_turns_report_the_latest_run_for_the_page_header(client, authed):
    _key_obj, raw, user = authed
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/project", ref="main", user=user
    )
    await Run.objects.acreate(session=session, trigger_type=SessionOrigin.UI_JOB, repo_id="group/project")
    latest = await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.UI_JOB,
        repo_id="group/project",
        status=RunStatus.FAILED,
        prompt="fix it",
    )
    hydrated = HydratedThread([], False, None, None, None)

    with (
        patch("sessions.api.views.ahydrate_thread", AsyncMock(return_value=hydrated)),
        patch("sessions.header.can_run", return_value=True),
    ):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.json()["latest_run"] == {
        "marker_id": f"run-status-{latest.id}",
        "label": "Failed",
        "variant": "failed",
        "finished_at": "",
        "retry": "fix it",
    }
    assert "watch" not in resp.json()
    assert resp.json()["run_count"] == 2


# ---------------------------------------------------------------------------
# session actions: rename, CI watch, archive
# ---------------------------------------------------------------------------


def _watch_thread_id(iid: int, repo_id: str = "group/project") -> str:
    from sessions.ids import compute_thread_id

    from codebase.base import Scope

    return compute_thread_id(repo_slug=repo_id, scope=Scope.MERGE_REQUEST, entity_iid=iid)


TURN_ON = {"on": True, "merge_request_iid": 7, "seen_state": "off"}


def _turn_off(seen_state: str = "watching") -> dict:
    return {"on": False, "merge_request_iid": 7, "seen_state": seen_state}


@pytest.fixture
def no_hydration():
    """The toggle names its merge request, so reading the thread for one would be wasted work."""
    with patch("sessions.api.views.ahydrate_thread", AsyncMock(side_effect=AssertionError("hydrated"))):
        yield


@pytest.fixture
def watch_world(monkeypatch):
    """The CI watch's outside world: the policy switch, the merge request's branch, and the queued evaluations."""
    from types import SimpleNamespace

    from sessions.pipeline_watch.policy import WatchPolicy
    from sessions.pipeline_watch.service import PipelineWatch

    from codebase.repo_config import RepositoryConfig

    world = SimpleNamespace(enabled=True, branch="daiv/branch", read_error=None, evaluations=[])

    async def aread_merge_request(merge_request_iid):
        if world.read_error is not None:
            raise world.read_error
        return SimpleNamespace(merge_request_id=merge_request_iid, source_branch=world.branch)

    def make(repo_id):
        config = RepositoryConfig(**{"pipeline_watch": {"enabled": world.enabled}})
        platform = SimpleNamespace(aread_merge_request=aread_merge_request)
        return PipelineWatch(repo_id, platform=platform, policy=WatchPolicy(config))

    class FakeTask:
        async def aenqueue(self, **kwargs):
            world.evaluations.append(kwargs)

    monkeypatch.setattr("sessions.api.views.PipelineWatch", make)
    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", FakeTask())
    return world


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("patch", "", {"title": "New"}), ("post", "/ci-watch", TURN_ON), ("post", "/archive", {"archived": True})],
)
def test_session_actions_reject_anonymous_callers(method, path, body):
    from django.test import Client

    resp = getattr(Client(), method)(f"/api/sessions/{uuid.uuid4()}{path}", data=body, content_type="application/json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_session_actions_need_the_csrf_token_from_a_browser(member_user):
    """The page posts with the cookie session, so the token the page sends is what stops a cross-site write."""
    from django.middleware.csrf import CSRF_SESSION_KEY, _get_new_csrf_string
    from django.test import Client

    session = _create_session(user=member_user, title="Old")
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(member_user)
    url = f"/api/sessions/{session.thread_id}"

    refused = browser.patch(url, data={"title": "New"}, content_type="application/json")
    token = _get_new_csrf_string()
    store = browser.session
    store[CSRF_SESSION_KEY] = token
    store.save()
    accepted = browser.patch(url, data={"title": "New"}, content_type="application/json", HTTP_X_CSRFTOKEN=token)

    assert (refused.status_code, accepted.status_code) == (403, 200)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("path", "body"), [("", {"title": "New"}), ("/ci-watch", TURN_ON), ("/archive", {"archived": True})]
)
async def test_session_actions_404_on_a_session_the_caller_cannot_see(client, authed, watch_world, path, body):
    _key_obj, raw, _user = authed
    other = await User.objects.acreate_user(username="other-act", email="other-act@example.com", password="x")  # noqa: S106
    session = await sync_to_async(_create_session)(user=other, title="Theirs")
    method = client.patch if path == "" else client.post

    resp = await method(f"/sessions/{session.thread_id}{path}", json=body, headers=_auth_headers(raw))

    assert resp.status_code == 404
    await session.arefresh_from_db()
    assert session.title == "Theirs"
    assert not await HiddenSession.objects.aexists()
    assert not await Session.objects.filter(thread_id=_watch_thread_id(7)).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("path", "body"), [("", {"title": "New"}), ("/ci-watch", TURN_ON)])
async def test_session_actions_403_for_a_reader_who_cannot_run_on_the_repo(client, authed, watch_world, path, body):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user, title="Mine")
    method = client.patch if path == "" else client.post

    with patch("sessions.api.views.can_run", return_value=False):
        resp = await method(f"/sessions/{session.thread_id}{path}", json=body, headers=_auth_headers(raw))

    assert resp.status_code == 403
    await session.arefresh_from_db()
    assert session.title == "Mine"
    assert not await Session.objects.filter(thread_id=_watch_thread_id(7)).aexists()


@pytest.mark.django_db(transaction=True)
async def test_session_rename_trims_caps_and_saves_only_the_title(client, authed):
    from sessions.titling import MAX_TITLE_LENGTH

    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user, title="Old", ref="main")
    await Session.objects.filter(pk=session.pk).aupdate(ref="feature")

    resp = await client.patch(
        f"/sessions/{session.thread_id}", json={"title": "  " + "x" * 200 + "  "}, headers=_auth_headers(raw)
    )

    assert resp.status_code == 200
    assert resp.json() == {"title": "x" * MAX_TITLE_LENGTH}
    await session.arefresh_from_db()
    assert (session.title, session.ref) == ("x" * MAX_TITLE_LENGTH, "feature")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("title", ["", "   \n\t"])
async def test_session_rename_refuses_a_blank_title(client, authed, title):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user, title="Old")

    resp = await client.patch(f"/sessions/{session.thread_id}", json={"title": title}, headers=_auth_headers(raw))

    assert resp.status_code == 422
    await session.arefresh_from_db()
    assert session.title == "Old"


@pytest.mark.django_db(transaction=True)
async def test_session_archive_marks_and_unmarks_it_for_the_caller_alone(client, authed):
    _key_obj, raw, user = authed
    other = await User.objects.acreate_user(username="co-owner", email="co-owner@example.com", password="x")  # noqa: S106
    session = await sync_to_async(_create_session)(user=user)
    await HiddenSession.objects.acreate(session=session, user=other)
    url = f"/sessions/{session.thread_id}/archive"

    archived = await client.post(url, json={"archived": True}, headers=_auth_headers(raw))
    again = await client.post(url, json={"archived": True}, headers=_auth_headers(raw))
    marked = [u async for u in HiddenSession.objects.order_by("user_id").values_list("user_id", flat=True)]
    restored = await client.post(url, json={"archived": False}, headers=_auth_headers(raw))

    assert (archived.json(), again.json(), restored.json()) == (
        {"archived": True},
        {"archived": True},
        {"archived": False},
    )
    assert marked == sorted([user.pk, other.pk])
    assert [u async for u in HiddenSession.objects.values_list("user_id", flat=True)] == [other.pk]


@pytest.mark.django_db(transaction=True)
async def test_session_archive_needs_no_run_access(client, authed):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)

    with patch("sessions.api.views.can_run", return_value=False):
        resp = await client.post(
            f"/sessions/{session.thread_id}/archive", json={"archived": True}, headers=_auth_headers(raw)
        )

    assert resp.status_code == 200
    assert await HiddenSession.objects.filter(session=session, user=user).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["watching", "fixing", "green", "exhausted", "unclear"])
async def test_session_ci_watch_off_ends_the_watch_on_the_merge_requests_thread(
    client, authed, watch_world, no_hydration, state
):
    """A chat session that published the merge request shows and toggles the watch its MR thread holds."""
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)
    watch_row = await sync_to_async(_create_session)(
        thread_id=_watch_thread_id(7), origin=SessionOrigin.MR_WEBHOOK, merge_request_iid=7, watch_state=state
    )

    resp = await client.post(
        f"/sessions/{session.thread_id}/ci-watch", json=_turn_off(state), headers=_auth_headers(raw)
    )

    assert resp.status_code == 200
    assert resp.json() is None
    await watch_row.arefresh_from_db()
    assert watch_row.watch_state == "off"


@pytest.mark.django_db(transaction=True)
async def test_session_ci_watch_off_loses_to_a_fix_run_that_claimed_the_watch_after_the_page_loaded(
    client, authed, watch_world
):
    """The page still shows "waiting for CI"; a fix run has since claimed the watch, and turning it off now loses."""
    from sessions.models import WatchState

    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(
        user=user,
        origin=SessionOrigin.MR_WEBHOOK,
        thread_id=_watch_thread_id(7),
        merge_request_iid=7,
        watch_state=WatchState.FIXING,
    )

    resp = await client.post(
        f"/sessions/{session.thread_id}/ci-watch", json=_turn_off("watching"), headers=_auth_headers(raw)
    )

    assert resp.status_code == 409
    assert resp.json()["reason"] == "changed"
    assert resp.json()["watch"] == {"state": "fixing", "label": "Fixing", "tone": "attn", "open": True}
    await session.arefresh_from_db()
    assert session.watch_state == WatchState.FIXING


@pytest.mark.django_db(transaction=True)
async def test_session_ci_watch_on_arms_the_merge_request_the_page_names_and_asks_for_an_evaluation(
    client, authed, watch_world, no_hydration
):
    """No merge request is on record for the session: the page found this one by its branch."""
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)
    watch_world.branch = "daiv/renamed"

    resp = await client.post(f"/sessions/{session.thread_id}/ci-watch", json=TURN_ON, headers=_auth_headers(raw))

    assert resp.status_code == 200
    assert resp.json() == {"state": "watching", "label": "Waiting for CI", "tone": "attn", "open": True}
    watch_row = await Session.objects.aget(thread_id=_watch_thread_id(7))
    assert (watch_row.ref, watch_row.user_id, watch_row.watch_attempts) == ("daiv/renamed", user.pk, 0)
    assert watch_world.evaluations == [{"repo_id": "group/project", "ref": "daiv/renamed"}]


@pytest.mark.django_db(transaction=True)
async def test_session_ci_watch_on_leaves_an_open_watch_alone(client, authed, watch_world):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)
    await sync_to_async(_create_session)(
        thread_id=_watch_thread_id(7),
        origin=SessionOrigin.MR_WEBHOOK,
        merge_request_iid=7,
        watch_state="fixing",
        watch_attempts=2,
    )

    resp = await client.post(f"/sessions/{session.thread_id}/ci-watch", json=TURN_ON, headers=_auth_headers(raw))

    assert resp.json()["state"] == "fixing"
    watch_row = await Session.objects.aget(thread_id=_watch_thread_id(7))
    assert watch_row.watch_attempts == 2
    assert watch_world.evaluations == []


@pytest.mark.django_db(transaction=True)
async def test_session_ci_watch_on_is_refused_when_the_repository_has_the_watch_off(client, authed, watch_world):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)
    watch_world.enabled = False

    resp = await client.post(f"/sessions/{session.thread_id}/ci-watch", json=TURN_ON, headers=_auth_headers(raw))

    assert resp.status_code == 409
    assert resp.json() == {"detail": "CI fixes are turned off for this repository", "reason": "disabled", "watch": None}
    assert not await Session.objects.filter(thread_id=_watch_thread_id(7)).aexists()


@pytest.mark.django_db(transaction=True)
async def test_session_ci_watch_on_reports_an_unreadable_merge_request(client, authed, watch_world):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)
    watch_world.read_error = RuntimeError("platform down")

    resp = await client.post(f"/sessions/{session.thread_id}/ci-watch", json=TURN_ON, headers=_auth_headers(raw))

    assert resp.status_code == 502


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "body",
    [
        {"on": True, "seen_state": "off"},
        {"on": True, "merge_request_iid": 0, "seen_state": "off"},
        {"on": False, "merge_request_iid": 7},
        {"on": False, "merge_request_iid": 7, "seen_state": "bogus"},
    ],
)
async def test_session_ci_watch_needs_the_merge_request_and_the_state_the_page_showed(
    client, authed, watch_world, body
):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)

    resp = await client.post(f"/sessions/{session.thread_id}/ci-watch", json=body, headers=_auth_headers(raw))

    assert resp.status_code == 422
    assert not await Session.objects.filter(thread_id=_watch_thread_id(7)).aexists()


@pytest.mark.django_db(transaction=True)
async def test_session_turns_report_the_watch_the_merge_requests_thread_holds(client, authed):
    _key_obj, raw, user = authed
    session = await sync_to_async(_create_session)(user=user)
    await sync_to_async(_create_session)(
        thread_id=_watch_thread_id(7), origin=SessionOrigin.MR_WEBHOOK, merge_request_iid=7, watch_state="green"
    )

    hydrated = HydratedThread([], False, {"id": 7, "source_branch": "daiv/branch"}, None, None)
    with patch("sessions.api.views.ahydrate_thread", AsyncMock(return_value=hydrated)):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.json()["watch"] == {"state": "green", "label": "Green", "tone": "clear", "open": False}
