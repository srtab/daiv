"""What the session page shows, built by one loader for both its render (``SessionDetailView``) and the turns endpoint
that keeps an open page current (``session_turns``), so the two cannot drift.

The session's revision (``core.ui_events``) is read before anything else, so a change that lands while the state is
built costs the page one extra fetch, never a missed one. A merge request the checkpoint does not carry is looked up
on the git platform on every render, but on a fetch only once nothing is in flight: a routine fetch while a run goes
never calls the platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async

from codebase.authorization import can_run
from core.ui_events import aread_session_revision
from sessions.executor.lock import background_in_flight, live_run_for, session_activity
from sessions.header import build_session_header, delegations_for, retry_for
from sessions.hydration import ahydrate_thread
from sessions.inbox.timeline import Timeline
from sessions.models import Run
from sessions.repo_state import aget_existing_mr_payload
from sessions.templatetags.session_tags import session_title
from sessions.transcript import (
    annotate_transcript,
    expired_run_turns,
    interleave_inbox_events,
    mark_run_openings,
    mark_skill_links,
)
from sessions.turns import build_turns
from sessions.up_next import up_next_payload

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.executor.lock import LiveRun, SessionActivity
    from sessions.hydration import HydratedThread
    from sessions.models import Session

_HEADER_KEYS = ("latest_run", "run_count", "delegations", "ci", "watch", "up_next")


@dataclass(frozen=True)
class SessionState:
    """A session as ``user`` sees it on its page.

    ``runs`` are oldest first. ``live_run`` is the slot holder as the viewer sees it (``live_run_for``). ``header`` is
    :func:`~sessions.header.build_session_header` plus ``up_next``. ``expired_banner`` is whether the page says the
    session expired: no checkpoint or archive, nothing in flight, and no failed run whose prompt or marker
    ``annotate_transcript`` recovers.
    """

    revision: int
    turns: list[dict[str, Any]]
    runs: list[Run]
    activity: SessionActivity
    hydrated: HydratedThread
    merge_request: dict | None
    thread_ref: str
    live_run: LiveRun | None
    header: dict[str, Any]
    can_act: bool
    expired_banner: bool


async def aload_session_state(session: Session, user: User, *, always_look_up_merge_request: bool) -> SessionState:
    """``session``'s :class:`SessionState` for ``user``; ``always_look_up_merge_request`` asks the platform for a
    merge request the checkpoint lacks even while a run is in flight."""
    revision = await aread_session_revision(session.thread_id)
    hydrated = await ahydrate_thread(session.thread_id)
    runs = [run async for run in Run.objects.filter(session=session).order_by("created_at")]
    activity = session_activity(session, runs)

    merge_request = hydrated.merge_request_payload
    look_up = always_look_up_merge_request or not activity.in_flight
    if merge_request is None and look_up and session.repo_id and session.ref:
        merge_request = await aget_existing_mr_payload(session.repo_id, session.ref)

    annotated = annotate_transcript(build_turns(hydrated.messages, in_flight=activity.in_flight), runs, activity)
    expired_banner = hydrated.expired and not activity.in_flight and not annotated

    def finish() -> tuple[list[dict[str, Any]], dict[str, Any], bool, LiveRun | None]:
        turns = interleave_inbox_events(
            annotated if hydrated.messages else annotated + expired_run_turns(runs, annotated),
            runs,
            Timeline(session.thread_id).events(),
        )
        mark_run_openings(turns, Timeline(session.thread_id).opening_events())
        mark_skill_links(turns, session.thread_id)
        can_act = can_run(user, session.repo_id)
        delegations = delegations_for(turns, session, user)
        retry = retry_for(session, runs, turns, user, can_act=can_act)
        header = build_session_header(
            session, runs, retry=retry, user=user, delegations=delegations, merge_request=merge_request
        )
        header["up_next"] = up_next_payload(session, runs, user, activity)
        return turns, header, can_act, live_run_for(activity.live, session, user)

    turns, header, can_act, live_run = await sync_to_async(finish)()
    return SessionState(
        revision=revision,
        turns=turns,
        runs=runs,
        activity=activity,
        hydrated=hydrated,
        merge_request=merge_request,
        # The pill is the working branch, as the live stream's ``_applyRepoState`` couples it to the merge request;
        # ``Session.ref`` lags it on any run that never synced back.
        thread_ref=(merge_request or {}).get("source_branch") or session.ref,
        live_run=live_run,
        header=header,
        can_act=can_act,
        expired_banner=expired_banner,
    )


def turns_payload(state: SessionState, session: Session) -> dict[str, Any]:
    """The turns endpoint's body: what an open page applies to stay current without a reload.

    ``expired`` is the hydration's own flag, not the page's ``expired_banner``.
    """
    activity, live = state.activity, state.live_run
    return {
        "revision": state.revision,
        "turns": state.turns,
        "active": activity.in_flight,
        "background": background_in_flight(activity, state.runs),
        "waiting_since": activity.waiting_since.isoformat() if activity.waiting_since else None,
        "expired": state.hydrated.expired,
        "live_run": {"id": live.id, "stoppable": live.stoppable, "started_at": live.started_at.isoformat()}
        if live
        else None,
        **{key: state.header[key] for key in _HEADER_KEYS},
        "merge_request": state.merge_request,
        "diff_stats": state.hydrated.diff_stats,
        "context_usage": state.hydrated.context_usage,
        "thread_ref": state.thread_ref,
        "title": session_title(session),
    }
