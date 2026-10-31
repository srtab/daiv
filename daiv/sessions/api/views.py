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
from sessions.header import aci_payload, latest_run_payload, retry_for, watch_payload
from sessions.hydration import ahydrate_thread
from sessions.inbox.drain import next_runs
from sessions.inbox.render import remembered_events, summarize
from sessions.models import EventState, HiddenSession, InboxEvent, MergeRequestOwnership, Session
from sessions.pipeline_watch.service import PipelineWatch
from sessions.titling import MAX_TITLE_LENGTH
from sessions.transcript import annotate_transcript, expired_run_turns, interleave_inbox_events, mark_skill_links
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
    merge_request_iid: int | None = Field(default=None, gt=0)


class ArchiveIn(Schema):
    archived: bool


class InboxMuteIn(Schema):
    muted: bool


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


@sessions_router.post("/{thread_id}/ci-watch", response={200: dict, 409: dict}, url_name="session_ci_watch")
async def session_ci_watch(request: HttpRequest, thread_id: str, payload: CIWatchIn):
    """Turn the CI watch on one merge request the session owns on or off, returning the session's CI as the page shows
    it (``ci``, and ``watch`` in the single-watch shape).

    ``merge_request_iid`` defaults to the session's only open merge request. Turning the watch on gives it its attempts
    back. A 409 carries the session's CI and a ``reason``: ``not_owned`` when the session does not own that merge
    request, ``disabled`` when the repository has the watch switched off.
    """
    session = await _get_runnable_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    owned = MergeRequestOwnership.objects.filter(session=session, closed_at__isnull=True)
    if payload.merge_request_iid is not None:
        owned = owned.filter(merge_request_iid=payload.merge_request_iid)
    ownerships = [ownership async for ownership in owned[:2]]
    if len(ownerships) != 1:
        refusal = {"detail": "This session doesn't own that merge request", "reason": "not_owned"}
        return 409, refusal | await _ci(session)
    watch = PipelineWatch(session.repo_id)
    if not payload.on:
        await watch.aturn_off(ownerships[0])
    elif not await watch.aturn_on(ownerships[0]):
        return 409, {"detail": "CI fixes are turned off for this repository", "reason": "disabled"} | await _ci(session)
    return await _ci(session)


async def _ci(session: Session) -> dict:
    ci = await aci_payload(session)
    return {"ci": ci, "watch": watch_payload(ci)}


@sessions_router.post("/{thread_id}/inbox-mute", response=dict, url_name="session_inbox_mute")
async def session_inbox_mute(request: HttpRequest, thread_id: str, payload: InboxMuteIn):
    """Mute or unmute the session's inbox: while muted, CI results and finished delegated batches are only
    remembered, and people's requests still start runs. Only the session's owner or an admin may; anyone else gets
    not found."""
    user = request.auth  # ty: ignore[unresolved-attribute]
    if not await Session.objects.by_owner(user).filter(thread_id=thread_id).aupdate(inbox_muted=payload.muted):
        raise HttpError(404, "Session not found")
    return {"muted": payload.muted}


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
    tells a page that rendered idle whether a run that started since is one it should follow; ``latest_run``, ``ci``
    (with ``watch``, its single-watch shape for older pages) and ``run_count`` keep the page's top bar and details
    panel current. ``queued`` is the session's next run and what is waiting for it, ``None`` when nothing is."""
    session = await _get_visible_session(request.auth, thread_id)  # ty: ignore[unresolved-attribute]
    hydrated = await ahydrate_thread(thread_id)
    runs = [r async for r in session.runs.order_by("created_at")]
    activity = session_activity(session, runs)
    turns = annotate_transcript(build_turns(hydrated.messages, in_flight=activity.in_flight), runs, activity)
    user = request.auth  # ty: ignore[unresolved-attribute]

    def finish_turns():
        if not hydrated.messages:
            turns.extend(expired_run_turns(runs, turns))
        turns[:] = interleave_inbox_events(turns, runs, remembered_events(session.thread_id))
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
    response |= await _ci(session)
    response["queued"] = await sync_to_async(queued_payload)(session)
    return response


def queued_payload(session: Session) -> dict | None:
    """SYNC ONLY: the session's next run and the events waiting for it, oldest first."""
    next_run = next_runs(session).first()
    if next_run is None:
        return None
    events = InboxEvent.objects.filter(run=next_run, state=EventState.PENDING).select_related("author")
    return {
        "run_id": str(next_run.pk),
        "events": [
            {"id": str(event.pk), "kind": event.kind, "summary": summarize(event), "created_at": event.created_at}
            for event in events.order_by("created_at", "id")
        ],
    }
