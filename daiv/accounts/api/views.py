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

Both counts are read through the same helpers that render the first page load
(``accounts.context_processors.query_running_jobs``,
``notifications.context_processors.query_unread_count``), so the seeded values and
the streamed ones can't drift apart.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from django.db import close_old_connections

from asgiref.sync import sync_to_async
from ninja import Router
from ninja.security import django_auth
from notifications.context_processors import query_unread_count

from accounts.context_processors import query_running_jobs
from core.sse import KEEP_ALIVE_FRAME, STREAM_MAX_DURATION_S, end_frame, retry_frame, sse_response
from core.ui_events import UIEventStream

if TYPE_CHECKING:
    from django.http import HttpRequest

    from accounts.models import User

logger = logging.getLogger("daiv.accounts")

nav_router = Router(tags=["nav"], auth=django_auth)

# Reader tuning. Waking every 20s keeps proxies and load balancers from reaping an
# idle stream (the wake costs a comment frame, no queries). The shared duration cap
# closes the response without a terminal frame, which is what makes EventSource
# reconnect — and the fresh snapshot on that reconnect is the resync.
POKE_WAIT_S = 20.0


def _counts(user: User) -> dict[str, int]:
    """Both badge counts in one trip to the DB thread.

    Sync because ``visible_to`` is (it resolves platform identity mid-query-build), and
    because Django's async ORM is ``sync_to_async`` underneath anyway — one hop beats two
    onto the same thread-sensitive executor. The connection goes back to the pool on the
    way out: a stream idles here for up to the duration cap, and one it holds without
    using is one ``DB_POOL_MAX_SIZE`` cannot give a request that would use it.
    """
    try:
        return {"unread_count": query_unread_count(user), "running_runs": query_running_jobs(user)}
    finally:
        close_old_connections()


async def _snapshot(user: User) -> dict[str, int]:
    return await sync_to_async(_counts)(user)


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
    yield retry_frame(3000)
    start = time.monotonic()
    try:
        # Subscribe before the first snapshot, not after: a run finishing in between
        # would otherwise be missed until the next reconnect. Pokes that land during the
        # snapshot queue up and cost one redundant recount on the first read.
        async with UIEventStream.for_user(user.pk) as events:
            state = await _snapshot(user)
            yield f"event: snapshot\ndata: {json.dumps(state)}\n\n"

            while (time.monotonic() - start) < STREAM_MAX_DURATION_S:
                if not await events.wait_for_change(POKE_WAIT_S):
                    yield KEEP_ALIVE_FRAME
                    continue

                fresh = await _snapshot(user)
                if fresh != state:
                    state = fresh
                    yield f"event: snapshot\ndata: {json.dumps(state)}\n\n"
    except Exception:
        logger.exception("nav: event stream failed for user pk=%s", user.pk)
        yield end_frame("error")


@nav_router.get("/events", url_name="nav_events")
async def nav_events(request: HttpRequest):
    """Live counters for the dashboard shell (notification bell, running-runs badge)."""
    return sse_response(_nav_frames(request.auth))  # ty: ignore[unresolved-attribute]
