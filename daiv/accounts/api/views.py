"""SSE stream for the dashboard shell's live counters.

The nav badges — unread notifications in the header bell, running runs in the
sidebar — used to be refreshed by a 10s HTMX poll per tab. One long-lived stream
replaces it: the first frame carries a snapshot, and thereafter a frame is sent
only when a ``core.ui_events`` poke means a count actually moved.

Why a snapshot rather than a delta log: the payload *is* the whole state (two
integers), so a reconnect needs no ``Last-Event-ID`` replay — it just resends the
truth. That makes the duration cap do double duty: it bounds how long a worker
stays occupied by one tab, and the reconnect it forces is the periodic resync
that repairs anything a dropped poke lost.

The counts are computed with the same helpers that render the first page load
(``accounts.context_processors.query_running_jobs``), so the seeded values and the
streamed ones can't drift apart.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from django.http import StreamingHttpResponse

from asgiref.sync import sync_to_async
from ninja import Router
from ninja.security import django_auth
from notifications.models import Notification

from accounts.context_processors import query_running_jobs
from core import ui_events

if TYPE_CHECKING:
    from django.http import HttpRequest

    from accounts.models import User

logger = logging.getLogger("daiv.accounts")

nav_router = Router(tags=["nav"], auth=django_auth)

# Reader tuning. Waking every 20s keeps proxies and load balancers from reaping an
# idle stream (the wake costs a comment frame, no queries). The 300s cap closes the
# response without a terminal frame, which is what makes EventSource reconnect — and
# the fresh snapshot on that reconnect is the resync, so the cap is load-bearing.
POKE_WAIT_S = 20.0
STREAM_MAX_DURATION_S = 300.0
# A finished run pokes several times in a row (status write, then the dispatcher's
# follow-ups). Collecting the burst before recomputing turns those into one snapshot.
POKE_COALESCE_S = 0.15


async def _unread_count(user: User) -> int:
    return await Notification.objects.filter(recipient=user, read_at__isnull=True).acount()


async def _snapshot(user: User) -> dict[str, int]:
    return {
        "unread_count": await _unread_count(user),
        # ``visible_to`` is sync-only (it resolves platform identity mid-query-build).
        "running_runs": await sync_to_async(query_running_jobs)(user),
    }


async def _nav_frames(user: User):
    """Snapshot on connect, then one frame per real change until the duration cap.

    Both counts are recomputed rather than carried in the poke: the running count is
    per-viewer (``Run.objects.visible_to``), so only the reader can know its own value.
    A frame is withheld when the recount matches what this connection last sent, which
    is what keeps the deliberately over-eager publishers from waking the client.

    An unexpected failure mid-stream ends the stream with an explicit ``event: end``
    instead of an unframed abort: to EventSource the latter is indistinguishable from a
    transient drop, so it would reconnect against a still-broken backend forever.
    """
    yield "retry: 3000\n\n"
    start = time.monotonic()
    try:
        # Subscribe before the first snapshot, not after: a run finishing in between
        # would otherwise be missed until the next reconnect. Pokes that land during the
        # snapshot queue up and cost one redundant recount on the first read.
        async with ui_events.subscription(ui_events.RUNS_CHANNEL, ui_events.user_channel(user.pk)) as pubsub:
            state = await _snapshot(user)
            yield f"event: snapshot\ndata: {json.dumps(state)}\n\n"

            while (time.monotonic() - start) < STREAM_MAX_DURATION_S:
                message = await pubsub.get_message(timeout=POKE_WAIT_S)
                if ui_events.parse_kind(message) is None:
                    yield ": keep-alive\n\n"
                    continue

                # Drain the rest of the burst before spending queries on it. The kind is
                # not consulted: one recount covers both counters, which also means a
                # poke of either kind repairs a badge whose own poke was dropped.
                while await pubsub.get_message(timeout=POKE_COALESCE_S) is not None:
                    pass

                fresh = await _snapshot(user)
                if fresh != state:
                    state = fresh
                    yield f"event: snapshot\ndata: {json.dumps(state)}\n\n"
    except Exception:
        logger.exception("nav: event stream failed for user pk=%s", user.pk)
        yield f"event: end\ndata: {json.dumps({'reason': 'error'})}\n\n"


@nav_router.get("/events", url_name="nav_events")
async def nav_events(request: HttpRequest):
    """Live counters for the dashboard shell (notification bell, running-runs badge)."""
    return StreamingHttpResponse(
        _nav_frames(request.auth),  # ty: ignore[unresolved-attribute, invalid-argument-type]
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
