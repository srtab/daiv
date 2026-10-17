from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts.api.security import AuthBearer
from sessions.hydration import ahydrate_thread
from sessions.models import Session
from sessions.transcript import annotate_transcript, artifact_turns
from sessions.turns import build_turns

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.sessions")

sessions_router = Router(tags=["sessions"], auth=[AuthBearer(), django_auth])


async def _get_visible_session(user, thread_id: str) -> Session:
    session = await Session.objects.by_owner(user).filter(thread_id=thread_id).afirst()
    if session is None:
        raise HttpError(404, "Session not found")
    return session


@sessions_router.get("/{thread_id}/turns", response=dict, url_name="session_turns")
async def session_turns(request: HttpRequest, thread_id: str):
    """Re-hydrated transcript for live background runs (the detail page polls this
    while a non-chat run holds the session slot)."""
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await _get_visible_session(user, thread_id)
    hydrated = await ahydrate_thread(thread_id)
    runs = [r async for r in session.runs.order_by("created_at")]
    turns = [] if hydrated.expired else annotate_transcript(build_turns(hydrated.messages), runs)
    if not hydrated.messages:
        turns += await sync_to_async(artifact_turns)(runs)
    # Authorised before the reads above; a fetch that restricted the session since must not be returned.
    if not await Session.objects.cross_project_visible_to(user).filter(pk=thread_id).aexists():
        raise HttpError(404, "Session not found")
    return {"turns": turns, "active": bool(session.active_run_id), "expired": hydrated.expired}
