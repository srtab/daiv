"""SSE stream for the dashboard shell's live counters.

The nav badges (unread notifications, running runs) are fed by one long-lived stream
per tab: the first frame carries a snapshot, and thereafter a frame is sent only when
a ``core.ui_events`` poke means the snapshot actually moved. Beside the two counts it
carries the latest running run's start, which only session pages loaded before the
``session`` frames existed still watch, and the same count and start over the user's own
sessions, which the sidebar's lists refetch on. A session page subscribes with ``?session=<thread_id>``,
and the same stream then also carries ``event: session`` frames with that session's
change revision, so the page needs no connection of its own to hear that it changed.

Why a snapshot rather than a delta log: the payload *is* the whole state (three
integers and two timestamps), so a reconnect needs no ``Last-Event-ID`` replay — it just
resends the truth. That makes the duration cap do double duty: it bounds how long a worker
stays occupied by one tab, and the reconnect it forces is the periodic resync
that repairs anything a dropped poke lost. Every failure short of "this
deployment has no bus at all" therefore closes *without* a terminal frame, so
that same reconnect is also the recovery path.

The snapshot is computed by the same helpers that render the first page load
(``dashboard.context_processors.running_snapshot``,
``notifications.context_processors.query_unread_count``), so the seeded values and
the streamed ones can't drift apart. The page render reaches the first through
``query_running_snapshot``; the stream hoists that helper's queryset out of the loop.
"""

from __future__ import annotations

import logging
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from django.db import Error as DatabaseError
from django.db import close_old_connections

from asgiref.sync import sync_to_async
from ninja import Router
from ninja.security import django_auth
from notifications.context_processors import query_unread_count
from sessions.models import Run, Session

from core.sse import KEEP_ALIVE_FRAME, STREAM_MAX_DURATION_S, data_frame, end_frame, retry_frame, sse_response
from core.ui_events import UIEventKind, UIEventStream, aread_session_revision, is_transient_bus_error, redis_connections
from dashboard.context_processors import running_snapshot

if TYPE_CHECKING:
    from django.http import HttpRequest

    from accounts.models import User

logger = logging.getLogger("daiv.dashboard")

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
        return Run.objects.visible_to(user)
    finally:
        close_old_connections()


@_in_db_thread
def _snapshot(user: User, visible) -> dict[str, Any] | None:
    """The whole nav state in one trip to the DB thread, or ``None`` when the DB failed.

    Sync because the querysets are (Django's async ORM is ``sync_to_async`` underneath
    anyway) — one hop beats two. The connection goes back to the pool on the way out: a
    stream idles for up to the duration cap, and one it holds without using is one
    ``DB_POOL_MAX_SIZE`` cannot give a request that would use it.

    ``None`` rather than zeros: the caller cannot tell a fabricated zero from a real one,
    and pushing one would blank a badge that is still correct in the browser.
    """
    try:
        return {"unread_count": query_unread_count(user), **running_snapshot(visible, user)}
    except DatabaseError as err:
        logger.warning("nav: recount failed for user pk=%s: %s", user.pk, err)
        return None
    finally:
        close_old_connections()


@_in_db_thread
def _session_is_visible(user: User, thread_id: str) -> bool:
    """Whether ``user`` may view the session, so one tab cannot subscribe to another's.

    A failed lookup reads as "no": the badges are unaffected, and the page falls back to
    what it had before session frames existed.
    """
    try:
        return Session.objects.visible_to(user).filter(thread_id=thread_id).exists()
    except DatabaseError as err:
        logger.warning("nav: session visibility check failed for user pk=%s: %s", user.pk, err)
        return False
    finally:
        close_old_connections()


def _session_frame(revision: int) -> str:
    return data_frame({"revision": revision}, event="session")


async def _nav_frames(user: User, session: str = ""):
    """Snapshot on connect, then one frame per real change until the duration cap.

    Both counts are recomputed rather than carried in the poke: the running count is
    per-viewer (``Run.objects.visible_to``), so only the reader can know its own value.
    A frame is withheld when the recount matches what this connection last sent (or
    failed outright), which is what keeps the deliberately over-eager publishers from
    waking the client.

    ``session`` names the session page the tab is showing. When the user may view it, the
    stream also subscribes to its channel and sends ``event: session`` carrying the
    session's revision: once after the snapshot, and again after each burst that poked it.
    The revision is read after subscribing, so a poke between the read and the subscribe
    cannot be lost. A session the user cannot view, or that does not exist, is ignored.

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
        watched = session if session and await _session_is_visible(user, session) else None
        # Subscribe before the first snapshot, not after: a run finishing in between
        # would otherwise be missed until the next reconnect.
        async with UIEventStream.for_user(user.pk, session=watched) as events:
            visible = await _open_visible_runs(user)
            state = await _snapshot(user, visible)
            if state is not None:
                yield data_frame(state, event="snapshot")
            if watched is not None:
                yield _session_frame(await aread_session_revision(watched))

            while (time.monotonic() - start) < STREAM_MAX_DURATION_S:
                kinds = await events.wait_for_change(POKE_WAIT_S)
                if not kinds:
                    yield KEEP_ALIVE_FRAME
                    continue

                if kinds - {UIEventKind.SESSION}:
                    fresh = await _snapshot(user, visible)
                    if fresh is not None and fresh != state:
                        state = fresh
                        yield data_frame(state, event="snapshot")
                if watched is not None and UIEventKind.SESSION in kinds:
                    yield _session_frame(await aread_session_revision(watched))
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
async def nav_events(request: HttpRequest, session: str = ""):
    """Live counters for the dashboard shell (unread and running-runs badges).

    ``session`` is the thread id of the session page the tab is showing, if any; the
    stream then also carries that session's change revision.
    """
    return sse_response(_nav_frames(request.auth, session))  # ty: ignore[unresolved-attribute]
