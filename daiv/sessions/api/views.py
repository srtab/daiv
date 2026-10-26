from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts.api.security import AuthBearer
from sessions.executor.lock import background_in_flight, live_run_for, session_activity
from sessions.hydration import ahydrate_thread
from sessions.models import Session
from sessions.transcript import annotate_transcript, artifact_turns
from sessions.turns import build_turns

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.sessions")

sessions_router = Router(tags=["sessions"], auth=[AuthBearer(), django_auth])


async def _get_visible_session(user, thread_id: str) -> Session:
    """The same visibility as the detail page this endpoint feeds, so a repo reader's poll does not 404."""
    session = await sync_to_async(lambda: Session.objects.visible_to(user).filter(thread_id=thread_id).first())()
    if session is None:
        raise HttpError(404, "Session not found")
    return session


@sessions_router.get("/{thread_id}/turns", response=dict, url_name="session_turns")
async def session_turns(request: HttpRequest, thread_id: str):
    """Re-hydrated transcript of an in-flight session, which the detail page polls while no run streams to it.
    ``live_run`` names the slot holder, so the page can stream that run from the relay instead; ``background``
    tells a page that rendered idle whether a run that started since is one it should follow."""
    session = await _get_visible_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    hydrated = await ahydrate_thread(thread_id)
    runs = [r async for r in session.runs.order_by("created_at")]
    activity = session_activity(session, runs)
    turns = annotate_transcript(build_turns(hydrated.messages, in_flight=activity.in_flight), runs, activity)
    if not hydrated.messages:
        turns += await sync_to_async(artifact_turns)(runs)
    live = await sync_to_async(live_run_for)(activity.live, session, request.auth)  # ty: ignore[unresolved-attribute]
    return {
        "turns": turns,
        "active": activity.in_flight,
        "background": background_in_flight(activity, runs),
        "waiting_since": activity.waiting_since.isoformat() if activity.waiting_since else None,
        "expired": hydrated.expired,
        "live_run": {"id": live.id, "stoppable": live.stoppable, "started_at": live.started_at.isoformat()}
        if live
        else None,
    }
