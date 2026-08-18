"""Tests for the nav SSE stream that replaced the bell's 10s poll.

The frames are asserted by driving ``_nav_frames`` directly with a fake pub/sub: the
whole point of the endpoint is what it does *between* pokes — recompute, compare,
and stay silent when nothing moved — which a request/response test can't observe.
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
from core import ui_events


class FakePubSub:
    """Replays a scripted burst of pokes, then reports timeouts forever.

    ``None`` is how ``redis.asyncio`` signals "nothing within the timeout", so a
    drained script puts the reader on its keep-alive path — exactly the idle stream.
    """

    def __init__(self, messages: list[dict | None], error: Exception | None = None):
        self._messages = list(messages)
        self._error = error
        self.channels: tuple[str, ...] = ()
        self.closed = False

    async def subscribe(self, *channels):
        self.channels = channels

    async def get_message(self, timeout=None):
        if self._error is not None:
            raise self._error
        return self._messages.pop(0) if self._messages else None

    async def aclose(self):
        self.closed = True


def poke(kind: str = "runs") -> dict:
    return {"type": "message", "channel": "daiv:ui-events:runs", "data": json.dumps({"kind": kind})}


def fake_subscription(pubsub: FakePubSub):
    """Stand in for ``ui_events.subscription`` with a pubsub the test controls."""

    class _CM:
        async def __aenter__(self):
            return pubsub

        async def __aexit__(self, *exc):
            await pubsub.aclose()
            return False

    def _factory(*channels):
        pubsub.channels = channels
        return _CM()

    return _factory


async def read_frames(user, pubsub: FakePubSub, count: int) -> list[str]:
    """Pull ``count`` frames, then abandon the (endless) stream."""
    frames = []
    generator = _nav_frames(user)
    with patch.object(ui_events, "subscription", fake_subscription(pubsub)):
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
        frames = await read_frames(member_user, FakePubSub([]), count=2)
        assert frames[0] == "retry: 3000\n\n"
        assert frames[1].startswith("event: snapshot\n")

    async def test_the_snapshot_carries_both_counts(self, member_user):
        await Notification.objects.acreate(
            recipient=member_user, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        await start_run(member_user)
        frames = await read_frames(member_user, FakePubSub([]), count=2)
        assert snapshots(frames) == [{"unread_count": 1, "running_runs": 1}]

    async def test_only_running_runs_are_counted(self, member_user):
        await start_run(member_user, status=RunStatus.SUCCESSFUL)
        await start_run(member_user, status=RunStatus.QUEUED, repo_id="daiv/other")
        frames = await read_frames(member_user, FakePubSub([]), count=2)
        assert snapshots(frames)[0]["running_runs"] == 0

    async def test_another_users_notifications_are_not_counted(self, member_user):
        bob = await User.objects.acreate_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        await Notification.objects.acreate(
            recipient=bob, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        frames = await read_frames(member_user, FakePubSub([]), count=2)
        assert snapshots(frames)[0]["unread_count"] == 0

    async def test_listens_on_the_broadcast_channel_and_the_users_own(self, member_user):
        pubsub = FakePubSub([])
        await read_frames(member_user, pubsub, count=2)
        assert pubsub.channels == (ui_events.RUNS_CHANNEL, f"daiv:ui-events:user:{member_user.pk}")

    async def test_an_idle_stream_sends_keep_alives_not_snapshots(self, member_user):
        frames = await read_frames(member_user, FakePubSub([]), count=4)
        assert frames[2:] == [": keep-alive\n\n", ": keep-alive\n\n"]

    async def test_a_poke_that_changed_nothing_sends_no_second_snapshot(self, member_user):
        """The publishers are a deliberate over-approximation (any Run save touching
        status pokes every reader), so suppression here is what keeps the client quiet."""
        frames = await read_frames(member_user, FakePubSub([poke()]), count=3)
        assert len(snapshots(frames)) == 1
        assert frames[2] == ": keep-alive\n\n"

    async def test_a_poke_after_a_real_change_sends_a_fresh_snapshot(self, member_user):
        pubsub = FakePubSub([])
        generator = _nav_frames(member_user)
        with patch.object(ui_events, "subscription", fake_subscription(pubsub)):
            try:
                assert await anext(generator) == "retry: 3000\n\n"
                first = await anext(generator)
                assert json.loads(first.split("data: ", 1)[1])["running_runs"] == 0

                await start_run(member_user)
                pubsub._messages.append(poke())
                second = await anext(generator)
                assert json.loads(second.split("data: ", 1)[1])["running_runs"] == 1
            finally:
                await generator.aclose()

    async def test_a_burst_of_pokes_costs_one_recompute(self, member_user):
        """A finishing run pokes several times over; coalescing turns the burst into a
        single recount instead of one per poke, per connected reader."""
        pubsub = FakePubSub([poke(), poke(), poke()])
        with patch("accounts.api.views.query_running_jobs", return_value=0) as query:
            await read_frames(member_user, pubsub, count=3)
        assert query.call_count == 2  # the opening snapshot, then one for the whole burst

    async def test_a_failure_mid_stream_ends_it_explicitly(self, member_user, caplog):
        """An unframed abort is indistinguishable from a transient drop, so EventSource
        would reconnect against a still-broken backend forever."""
        pubsub = FakePubSub([], error=ConnectionError("redis went away"))
        frames = await read_frames(member_user, pubsub, count=3)
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
