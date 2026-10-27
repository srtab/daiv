from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from asgiref.sync import sync_to_async
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts.api.security import AuthBearer
from codebase.authorization import can_run
from sessions.executor.lock import background_in_flight, live_run_for, session_activity
from sessions.header import awatch_session, latest_run_payload, merge_request_iid, watch_payload
from sessions.hydration import ahydrate_thread
from sessions.models import Session, WatchState
from sessions.pipeline_watch.service import PipelineWatch
from sessions.titling import MAX_TITLE_LENGTH
from sessions.transcript import annotate_transcript, expired_run_turns, mark_retryable
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
    """A visible session whose repository ``user`` may run on: the rule Retry follows, for the page's other actions."""
    session = await _get_visible_session(user, thread_id)
    if not await sync_to_async(can_run)(user, session.repo_id):
        raise HttpError(403, "You need run access to this session's repository")
    return session


class TitleIn(Schema):
    title: str


class CIWatchIn(Schema):
    on: bool


class ArchiveIn(Schema):
    archived: bool


@sessions_router.patch("/{thread_id}", response=dict, url_name="session_update")
async def session_update(request: HttpRequest, thread_id: str, payload: TitleIn):
    """Rename the session. Blank titles are refused, so the page keeps the old one."""
    session = await _get_runnable_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    title = payload.title.strip()[:MAX_TITLE_LENGTH].strip()
    if not title:
        raise HttpError(422, "A title can't be blank")
    await Session.objects.filter(thread_id=session.thread_id).aupdate(title=title)
    return {"title": title}


@sessions_router.post("/{thread_id}/ci-watch", response={200: dict | None, 409: dict}, url_name="session_ci_watch")
async def session_ci_watch(request: HttpRequest, thread_id: str, payload: CIWatchIn):
    """Turn the CI watch on the session's merge request on or off, returning the watch as the page shows it.

    A 409 carries the watch as it now is and a ``reason``: ``changed`` when a fix run or a verdict moved the watch
    before it could be turned off, ``disabled`` when the repository has the watch switched off.
    """
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await _get_runnable_session(user, thread_id)
    hydrated = await ahydrate_thread(thread_id)
    runs = [r async for r in session.runs.order_by("created_at")]
    iid = merge_request_iid(session, runs, hydrated.merge_request_payload)
    if iid is None:
        raise HttpError(409, "This session has no merge request")

    watch = PipelineWatch(session.repo_id)
    row = await awatch_session(session, iid)
    state = row.watch_state if row is not None else WatchState.OFF
    if not payload.on:
        if not await watch.aturn_off(merge_request_iid=iid, seen_state=state):
            current = watch_payload(await awatch_session(session, iid))
            return 409, {
                "detail": "The watch changed before it could be turned off",
                "reason": "changed",
                "watch": current,
            }
    elif state not in WatchState.open():
        try:
            armed = await watch.aturn_on(
                merge_request_iid=iid, user_id=user.pk, sandbox_environment_id=session.sandbox_environment_id
            )
        except Exception as exc:
            logger.exception("Could not turn the CI watch on for %s!%s", session.repo_id, iid)
            raise HttpError(502, "Couldn't read the merge request; try again") from exc
        if armed is None:
            return 409, {"detail": "CI fixes are turned off for this repository", "reason": "disabled", "watch": None}
    return watch_payload(await awatch_session(session, iid))


@sessions_router.post("/{thread_id}/archive", response=dict, url_name="session_archive")
async def session_archive(request: HttpRequest, thread_id: str, payload: ArchiveIn):
    """Archive the session out of the list, or bring it back. The session itself stays usable either way."""
    session = await _get_runnable_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    hidden_at = timezone.now() if payload.archived else None
    await Session.objects.filter(thread_id=session.thread_id).aupdate(hidden_at=hidden_at)
    return {"archived": payload.archived, "hidden_at": hidden_at.isoformat() if hidden_at else None}


@sessions_router.get("/{thread_id}/turns", response=dict, url_name="session_turns")
async def session_turns(request: HttpRequest, thread_id: str):
    """Re-hydrated transcript of an in-flight session, which the detail page polls while no run streams to it.
    ``live_run`` names the slot holder, so the page can stream that run from the relay instead; ``background``
    tells a page that rendered idle whether a run that started since is one it should follow; ``latest_run`` and
    ``watch`` keep the page's top bar and details panel current."""
    session = await _get_visible_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    hydrated = await ahydrate_thread(thread_id)
    runs = [r async for r in session.runs.order_by("created_at")]
    activity = session_activity(session, runs)
    turns = annotate_transcript(build_turns(hydrated.messages, in_flight=activity.in_flight), runs, activity)
    user = request.auth  # ty: ignore[unresolved-attribute]

    def finish_turns():
        if not hydrated.messages:
            turns.extend(expired_run_turns(runs, turns))
        mark_retryable(turns, runs, user)
        return live_run_for(activity.live, session, user), latest_run_payload(runs, user)

    live, latest_run = await sync_to_async(finish_turns)()
    return {
        "turns": turns,
        "active": activity.in_flight,
        "background": background_in_flight(activity, runs),
        "waiting_since": activity.waiting_since.isoformat() if activity.waiting_since else None,
        "expired": hydrated.expired,
        "latest_run": latest_run,
        "watch": watch_payload(
            await awatch_session(session, merge_request_iid(session, runs, hydrated.merge_request_payload))
        ),
        "live_run": {"id": live.id, "stoppable": live.stoppable, "started_at": live.started_at.isoformat()}
        if live
        else None,
    }
