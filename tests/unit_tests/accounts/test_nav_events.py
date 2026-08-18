"""Tests for the nav SSE stream that replaced the bell's 10s poll.

The frames are asserted by driving ``_nav_frames`` directly with a fake bus stream: the
whole point of the endpoint is what it does *between* changes — recompute, compare, and
stay silent when nothing moved — which a request/response test can't observe. What the
bus itself does (channels, coalescing, junk) is pinned in ``core/test_ui_events.py``;
here it is only ever "something changed" or "nothing did".
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from notifications.models import Notification
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.api.views import _nav_frames
from accounts.models import User


class FakeStream:
    """Stands in for ``UIEventStream``: a scripted sequence of answers to
    ``wait_for_change``, then "nothing changed" forever — exactly the idle stream."""

    def __init__(self, changes: list[bool] | None = None, error: Exception | None = None):
        self.changes = list(changes or [])
        self.error = error
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def wait_for_change(self, timeout):
        if self.error is not None:
            raise self.error
        return self.changes.pop(0) if self.changes else False


async def read_frames(user, stream: FakeStream, count: int) -> list[str]:
    """Pull ``count`` frames, then abandon the (endless) stream."""
    frames = []
    generator = _nav_frames(user)
    with patch("accounts.api.views.UIEventStream.for_user", return_value=stream):
        try:
            async for frame in generator:
                frames.append(frame)
                if len(frames) >= count:
                    break
        finally:
            await generator.aclose()
    return frames


@sync_to_async
def start_run(user, *, status=RunStatus.RUNNING, repo_id="daiv/api") -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id=repo_id, user=user
    )
    return Run.objects.create(
        session=session, status=status, trigger_type=SessionOrigin.UI_JOB, repo_id=repo_id, user=user
    )


def snapshots(frames: list[str]) -> list[dict]:
    return [json.loads(frame.split("data: ", 1)[1].strip()) for frame in frames if frame.startswith("event: snapshot")]


@pytest.mark.django_db(transaction=True)
class TestNavFrames:
    async def test_opens_with_a_reconnect_hint_then_a_snapshot(self, member_user):
        """``retry:`` first because the duration cap works by *forcing* a reconnect —
        without it the browser would use its own back-off."""
        frames = await read_frames(member_user, FakeStream(), count=2)
        assert frames[0] == "retry: 3000\n\n"
        assert frames[1].startswith("event: snapshot\n")

    async def test_the_snapshot_carries_both_counts(self, member_user):
        await Notification.objects.acreate(
            recipient=member_user, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        await start_run(member_user)
        frames = await read_frames(member_user, FakeStream(), count=2)
        assert snapshots(frames) == [{"unread_count": 1, "running_runs": 1}]

    async def test_only_running_runs_are_counted(self, member_user):
        await start_run(member_user, status=RunStatus.SUCCESSFUL)
        await start_run(member_user, status=RunStatus.QUEUED, repo_id="daiv/other")
        frames = await read_frames(member_user, FakeStream(), count=2)
        assert snapshots(frames)[0]["running_runs"] == 0

    async def test_another_users_notifications_are_not_counted(self, member_user):
        bob = await User.objects.acreate_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        await Notification.objects.acreate(
            recipient=bob, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        frames = await read_frames(member_user, FakeStream(), count=2)
        assert snapshots(frames)[0]["unread_count"] == 0

    async def test_it_listens_for_the_pokes_this_viewer_needs(self, member_user):
        with patch("accounts.api.views.UIEventStream.for_user", return_value=FakeStream()) as for_user:
            generator = _nav_frames(member_user)
            try:
                await anext(generator)
                await anext(generator)
            finally:
                await generator.aclose()
        for_user.assert_called_once_with(member_user.pk)

    async def test_an_idle_stream_sends_keep_alives_not_snapshots(self, member_user):
        frames = await read_frames(member_user, FakeStream(), count=4)
        assert frames[2:] == [": keep-alive\n\n", ": keep-alive\n\n"]

    async def test_a_change_that_moved_no_count_sends_no_second_snapshot(self, member_user):
        """The publishers are a deliberate over-approximation (any Run save touching
        status pokes every reader), so suppression here is what keeps the client quiet."""
        frames = await read_frames(member_user, FakeStream([True]), count=3)
        assert len(snapshots(frames)) == 1
        assert frames[2] == ": keep-alive\n\n"

    async def test_a_change_to_a_count_sends_a_fresh_snapshot(self, member_user):
        stream = FakeStream()
        generator = _nav_frames(member_user)
        with patch("accounts.api.views.UIEventStream.for_user", return_value=stream):
            try:
                assert await anext(generator) == "retry: 3000\n\n"
                first = await anext(generator)
                assert json.loads(first.split("data: ", 1)[1])["running_runs"] == 0

                await start_run(member_user)
                stream.changes.append(True)
                second = await anext(generator)
                assert json.loads(second.split("data: ", 1)[1])["running_runs"] == 1
            finally:
                await generator.aclose()

    async def test_a_failure_mid_stream_ends_it_explicitly(self, member_user, caplog):
        """An unframed abort is indistinguishable from a transient drop, so EventSource
        would reconnect against a still-broken backend forever."""
        frames = await read_frames(member_user, FakeStream(error=ConnectionError("redis went away")), count=3)
        assert frames[2] == 'event: end\ndata: {"reason": "error"}\n\n'
        assert "event stream failed" in caplog.text


@pytest.mark.django_db
class TestNavEventsEndpoint:
    def test_anonymous_callers_are_rejected(self, client):
        response = client.get("/api/nav/events")
        assert response.status_code == 401

    def test_it_is_served_as_an_event_stream(self, member_client):
        with patch("accounts.api.views._nav_frames", return_value=iter([])):
            response = member_client.get("/api/nav/events")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/event-stream"
        # Without these nginx buffers the whole response and nothing streams.
        assert response["Cache-Control"] == "no-cache"
        assert response["X-Accel-Buffering"] == "no"
