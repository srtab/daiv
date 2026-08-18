"""SSE stream for the dashboard shell's live counters.

The nav badges — unread notifications in the header bell, running runs in the
sidebar — used to be refreshed by a 10s HTMX poll per tab. One long-lived stream
replaces it: the first frame carries a snapshot, and thereafter a frame is sent
only when a ``core.ui_events`` poke means a count actually moved.

Why a snapshot rather than a delta log: the payload *is* the whole state (two
integers), so a reconnect needs no ``Last-Event-ID`` replay — it just resends the
truth. That makes the duration cap do double duty: it bounds how long a worker
stays occupied by one tab, and the reconnect it forces is the periodic resync
that repairs anything a dropped poke lost. Every failure short of "this
deployment has no bus at all" therefore closes *without* a terminal frame, so
that same reconnect is also the recovery path.

Both counts are computed by the same helpers that render the first page load
(``accounts.context_processors.count_running``,
``notifications.context_processors.query_unread_count``), so the seeded values and
the streamed ones can't drift apart. The page render reaches the first through
``query_running_jobs``; the stream hoists that helper's queryset out of the loop.
"""

from __future__ import annotations

import logging
import time
from functools import partial
from typing import TYPE_CHECKING

from django.db import Error as DatabaseError
from django.db import close_old_connections

from asgiref.sync import sync_to_async
from ninja import Router
from ninja.security import django_auth
from notifications.context_processors import query_unread_count

from accounts.context_processors import count_running, visible_runs
from core.sse import KEEP_ALIVE_FRAME, STREAM_MAX_DURATION_S, data_frame, end_frame, retry_frame, sse_response
from core.ui_events import UIEventStream, is_transient_bus_error, redis_connections

if TYPE_CHECKING:
    from django.http import HttpRequest

    from accounts.models import User

logger = logging.getLogger("daiv.accounts")

nav_router = Router(tags=["nav"], auth=django_auth)

# Reader tuning. Waking every 20s keeps proxies and load balancers from reaping an
# idle stream (the wake costs a comment frame, no queries).
POKE_WAIT_S = 20.0

RETRY_MS = 3_000
# Back-off asked for before giving up on a failed stream, so the reconnect that
# recovers a transient fault can't hammer a lasting one.
FAILURE_RETRY_MS = 30_000


# ``thread_sensitive=False``: the default mints one executor thread per request context
# and holds it until the response ends, which here is the duration cap — an idle thread
# per open tab. These hops touch no shared sync state and span no transaction, so the
# loop's bounded default executor is enough.
_in_db_thread = partial(sync_to_async, thread_sensitive=False)


@_in_db_thread
def _open_visible_runs(user: User):
    """Resolve run visibility once per connection.

    ``visible_to`` costs a platform-identity read plus a cache round-trip to build, and
    none of that changes for the life of a stream; only the RUNNING count does.
    """
    try:
        return visible_runs(user)
    finally:
        close_old_connections()


@_in_db_thread
def _snapshot(user: User, visible) -> dict[str, int] | None:
    """Both badge counts in one trip to the DB thread, or ``None`` when the DB failed.

    Sync because the querysets are (Django's async ORM is ``sync_to_async`` underneath
    anyway) — one hop beats two. The connection goes back to the pool on the way out: a
    stream idles for up to the duration cap, and one it holds without using is one
    ``DB_POOL_MAX_SIZE`` cannot give a request that would use it.

    ``None`` rather than zeros: the caller cannot tell a fabricated zero from a real one,
    and pushing one would blank a badge that is still correct in the browser.
    """
    try:
        return {"unread_count": query_unread_count(user), "running_runs": count_running(visible)}
    except DatabaseError as err:
        logger.warning("nav: recount failed for user pk=%s: %s", user.pk, err)
        return None
    finally:
        close_old_connections()


async def _nav_frames(user: User):
    """Snapshot on connect, then one frame per real change until the duration cap.

    Both counts are recomputed rather than carried in the poke: the running count is
    per-viewer (``Run.objects.visible_to``), so only the reader can know its own value.
    A frame is withheld when the recount matches what this connection last sent (or
    failed outright), which is what keeps the deliberately over-eager publishers from
    waking the client.

    A deployment with no bus configured is told so with an explicit ``event: end`` —
    reconnecting cannot fix it. Anything else closes silently after asking for a longer
    retry, because to EventSource that is a dropped connection and the fresh snapshot on
    its reconnect is exactly the resync a transient fault needs.
    """
    yield retry_frame(RETRY_MS)
    if not redis_connections.configured:
        logger.warning("nav: event stream unavailable, DJANGO_REDIS_URL is not configured")
        yield end_frame("unavailable")
        return
    start = time.monotonic()
    try:
        # Subscribe before the first snapshot, not after: a run finishing in between
        # would otherwise be missed until the next reconnect.
        async with UIEventStream.for_user(user.pk) as events:
            visible = await _open_visible_runs(user)
            state = await _snapshot(user, visible)
            if state is not None:
                yield data_frame(state, event="snapshot")

            while (time.monotonic() - start) < STREAM_MAX_DURATION_S:
                if not await events.wait_for_change(POKE_WAIT_S):
                    yield KEEP_ALIVE_FRAME
                    continue

                fresh = await _snapshot(user, visible)
                if fresh is not None and fresh != state:
                    state = fresh
                    yield data_frame(state, event="snapshot")
    except Exception as err:
        # A dropped bus is anticipated, and the client is about to reconnect: at WARNING
        # without a traceback, an outage costs one line per tab per retry instead of a
        # Sentry error event. Only a real bug gets the traceback.
        if is_transient_bus_error(err):
            logger.warning("nav: event stream lost the bus for user pk=%s: %s", user.pk, err)
        else:
            logger.exception("nav: event stream failed for user pk=%s", user.pk)
        yield retry_frame(FAILURE_RETRY_MS)


@nav_router.get("/events", url_name="nav_events")
async def nav_events(request: HttpRequest):
    """Live counters for the dashboard shell (notification bell, running-runs badge)."""
    return sse_response(_nav_frames(request.auth))  # ty: ignore[unresolved-attribute]
