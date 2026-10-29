from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from ninja import Field, Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts.api.security import AuthBearer
from codebase.authorization import can_run
from sessions.executor.lock import background_in_flight, live_run_for, session_activity
from sessions.header import awatch_state, latest_run_payload, merge_request_iid, retry_for, watch_payload
from sessions.hydration import ahydrate_thread
from sessions.models import HiddenSession, Session, WatchState
from sessions.pipeline_watch.service import PipelineWatch
from sessions.titling import MAX_TITLE_LENGTH
from sessions.transcript import annotate_transcript, expired_run_turns, mark_skill_links
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
    merge_request_iid: int = Field(gt=0)
    seen_state: WatchState


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
    return {"title": title}


@sessions_router.post("/{thread_id}/ci-watch", response={200: dict | None, 409: dict}, url_name="session_ci_watch")
async def session_ci_watch(request: HttpRequest, thread_id: str, payload: CIWatchIn):
    """Turn the CI watch on the merge request the page shows on or off, returning the watch as the page shows it.

    The merge request is one of the session's repository, which is what run access is checked on. ``seen_state`` is
    the state the page showed, and turning off only moves a watch still in it. A 409 carries the watch as it now is
    and a ``reason``: ``changed`` when a fix run or a verdict moved the watch first, ``disabled`` when the repository
    has the watch switched off.
    """
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await _get_runnable_session(user, thread_id)
    iid = payload.merge_request_iid
    watch = PipelineWatch(session.repo_id)
    if not payload.on:
        if not await watch.aturn_off(merge_request_iid=iid, seen_state=payload.seen_state):
            return 409, {
                "detail": "The watch changed before it could be turned off",
                "reason": "changed",
                "watch": watch_payload(await watch.astate(iid)),
            }
    else:
        try:
            armed = await watch.aturn_on(
                merge_request_iid=iid, user_id=user.pk, sandbox_environment_id=session.sandbox_environment_id
            )
        except Exception as exc:
            logger.exception("Could not turn the CI watch on for %s!%s", session.repo_id, iid)
            raise HttpError(502, "Couldn't read the merge request; try again") from exc
        if armed is None:
            return 409, {"detail": "CI fixes are turned off for this repository", "reason": "disabled", "watch": None}
    return watch_payload(await watch.astate(iid))


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
    """Re-hydrated transcript of an in-flight session, which the detail page polls while no run streams to it.
    ``live_run`` names the slot holder, so the page can stream that run from the relay instead; ``background``
    tells a page that rendered idle whether a run that started since is one it should follow; ``latest_run``,
    ``watch`` and ``run_count`` keep the page's top bar and details panel current. ``watch`` is left out while no
    merge request is on record, so a page showing one found by its branch keeps the watch it has."""
    session = await _get_visible_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    hydrated = await ahydrate_thread(thread_id)
    runs = [r async for r in session.runs.order_by("created_at")]
    activity = session_activity(session, runs)
    turns = annotate_transcript(build_turns(hydrated.messages, in_flight=activity.in_flight), runs, activity)
    user = request.auth  # ty: ignore[unresolved-attribute]

    def finish_turns():
        if not hydrated.messages:
            turns.extend(expired_run_turns(runs, turns))
        mark_skill_links(turns, session.thread_id)
        retry = retry_for(session, runs, turns, user)
        return live_run_for(activity.live, session, user), latest_run_payload(runs, retry)

    live, latest_run = await sync_to_async(finish_turns)()
    response = {
        "turns": turns,
        "active": activity.in_flight,
        "background": background_in_flight(activity, runs),
        "waiting_since": activity.waiting_since.isoformat() if activity.waiting_since else None,
        "expired": hydrated.expired,
        "latest_run": latest_run,
        "run_count": len(runs),
        "live_run": {"id": live.id, "stoppable": live.stoppable, "started_at": live.started_at.isoformat()}
        if live
        else None,
    }
    if (iid := merge_request_iid(session, runs, hydrated.merge_request_payload)) is not None:
        response["watch"] = watch_payload(await awatch_state(session, iid))
    return response
