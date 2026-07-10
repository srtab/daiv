from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from chat.api.security import AuthBearer
from chat.turns import build_turns
from sessions.hydration import ahydrate_thread
from sessions.models import Session

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.sessions")

sessions_router = Router(tags=["sessions"], auth=[AuthBearer(), django_auth])


async def _get_visible_session(user, thread_id: str) -> Session:
    session = await Session.objects.by_owner(user).filter(thread_id=thread_id).afirst()
    if session is None:
        raise HttpError(404, "Session not found")
    return session


@sessions_router.get("/{thread_id}/status", response=dict, url_name="session_status")
async def session_status(request: HttpRequest, thread_id: str):
    """Cheap probe so a reloaded page can detect when the in-flight run released the slot."""
    session = await _get_visible_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    return {"active": bool(session.active_run_id)}


@sessions_router.get("/{thread_id}/turns", response=dict, url_name="session_turns")
async def session_turns(request: HttpRequest, thread_id: str):
    """Re-hydrated transcript for live background runs (the detail page polls this
    while a non-chat run holds the session slot)."""
    session = await _get_visible_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    messages, expired, _mr = await ahydrate_thread(thread_id)
    return {
        "turns": [] if expired else build_turns(messages),
        "active": bool(session.active_run_id),
        "expired": expired,
    }
