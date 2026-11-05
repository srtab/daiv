from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from ninja import Field, Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts.api.security import AuthBearer
from codebase.authorization import can_run
from sessions.header import aci_payload, watch_payload
from sessions.live.notify import asession_changed
from sessions.models import HiddenSession, MergeRequestOwnership, RunStatus, Session
from sessions.page_state import aload_session_state, turns_payload
from sessions.pipeline_watch.service import PipelineWatch
from sessions.titling import MAX_TITLE_LENGTH
from sessions.up_next import aup_next_payload

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.sessions")

sessions_router = Router(tags=["sessions"], auth=[AuthBearer(), django_auth])


async def _get_visible_session(user, thread_id: str) -> Session:
    """The same visibility as the detail page this endpoint feeds, so a repo reader's fetch does not 404."""
    session = await sync_to_async(lambda: Session.objects.visible_to(user).filter(thread_id=thread_id).first())()
    if session is None:
        raise HttpError(404, "Session not found")
    return session


async def _get_runnable_session(user, thread_id: str) -> Session:
    """A visible session whose repository ``user`` may run on: the rule the page's actions follow."""
    session = await _get_visible_session(user, thread_id)
    if not await sync_to_async(can_run)(user, session.repo_id):
        raise HttpError(403, "You need run access to this session's repository")
    return session


class TitleIn(Schema):
    title: str


class CIWatchIn(Schema):
    on: bool
    merge_request_iid: int | None = Field(default=None, gt=0)
    repo_id: str = ""


class ArchiveIn(Schema):
    archived: bool


@sessions_router.patch("/{thread_id}", response=dict, url_name="session_update")
async def session_update(request: HttpRequest, thread_id: str, payload: TitleIn):
    """Rename the session. Blank titles are refused, so the page keeps the old one."""
    session = await _get_runnable_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    title = payload.title.strip()[:MAX_TITLE_LENGTH].rstrip()
    if not title:
        raise HttpError(422, "A title can't be blank")
    session.title = title
    await session.asave(update_fields=["title"])
    await asession_changed(session.thread_id)
    return {"title": title}


@sessions_router.post("/{thread_id}/ci-watch", response={200: dict, 409: dict}, url_name="session_ci_watch")
async def session_ci_watch(request: HttpRequest, thread_id: str, payload: CIWatchIn):
    """Turn the CI watch on one merge request the session owns on or off, returning the session's CI as the page shows
    it (``ci``, and ``watch`` in the single-watch shape).

    ``repo_id`` is the merge request's repository, the session's own when blank, and ``merge_request_iid`` defaults to
    the session's only open merge request there. Turning the watch on gives it its attempts back. A 409 carries the
    session's CI and a ``reason``: ``not_owned`` when the session does not own that merge request, ``disabled`` when
    the repository has the watch switched off.
    """
    session = await _get_runnable_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    owned = MergeRequestOwnership.objects.filter(
        session=session, repo_id=payload.repo_id or session.repo_id, closed_at__isnull=True
    )
    if payload.merge_request_iid is not None:
        owned = owned.filter(merge_request_iid=payload.merge_request_iid)
    ownerships = [ownership async for ownership in owned[:2]]
    if len(ownerships) != 1:
        refusal = {"detail": "This session doesn't own that merge request", "reason": "not_owned"}
        return 409, refusal | await _ci(session)
    ownership = ownerships[0]
    if ownership.repo_id != session.repo_id and not await sync_to_async(can_run)(request.auth, ownership.repo_id):
        raise HttpError(403, "You need run access to this merge request's repository")
    watch = PipelineWatch(ownership.repo_id)
    if not payload.on:
        await watch.aturn_off(ownership)
    elif not await watch.aturn_on(ownership):
        return 409, {"detail": "CI fixes are turned off for this repository", "reason": "disabled"} | await _ci(session)
    return await _ci(session)


async def _ci(session: Session) -> dict:
    ci = await aci_payload(session)
    return {"ci": ci, "watch": watch_payload(ci)}


@sessions_router.post("/{thread_id}/archive", response=dict, url_name="session_archive")
async def session_archive(request: HttpRequest, thread_id: str, payload: ArchiveIn):
    """Archive the session out of the caller's own list, or bring it back; nobody else's list changes."""
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await _get_visible_session(user, thread_id)
    if payload.archived:
        await HiddenSession.objects.aget_or_create(session=session, user=user)
    else:
        await HiddenSession.objects.filter(session=session, user=user).adelete()
    return {"archived": payload.archived}


@sessions_router.get("/{thread_id}/turns", response=dict, url_name="session_turns")
async def session_turns(request: HttpRequest, thread_id: str):
    """The session as its page shows it (``sessions.page_state.turns_payload``), which an open page fetches when the
    nav stream announces a change."""
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await _get_visible_session(user, thread_id)
    return turns_payload(await aload_session_state(session, user, always_look_up_merge_request=False), session)


@sessions_router.get("/{thread_id}/up-next", response=dict, url_name="session_up_next")
async def session_up_next(request: HttpRequest, thread_id: str):
    """What the session will do next; kept for detail pages loaded before the nav stream carried ``session`` frames,
    which poll it while a run streams to them."""
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await _get_visible_session(user, thread_id)
    unfinished = session.runs.exclude(status__in=RunStatus.terminal()).select_related("user").order_by("created_at")
    return {"up_next": await aup_next_payload(session, [r async for r in unfinished], user)}
