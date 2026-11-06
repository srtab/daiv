"""What the session page shows, built by one loader for both its render (``SessionDetailView``) and the turns endpoint
that keeps an open page current (``session_turns``), so the two cannot drift.

The session's revision (``core.ui_events``) is read before anything else, so a change that lands while the state is
built costs the page one extra fetch, never a missed one. The caller's ``Session`` row is re-read after it for the same
reason: a slot claim or release, or a rename, changes only that row. A merge request the checkpoint does not carry is
looked up on the git platform on every render, but on a fetch only once nothing is in flight, and then from the answer
cached per branch for a minute, which each render refreshes. A fetch while a run goes never calls the platform; it, like
a lookup the platform failed, leaves ``merge_request`` out rather than report none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.cache import cache

from asgiref.sync import sync_to_async

from codebase.authorization import can_run
from codebase.clients.base import PLATFORM_ERRORS
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryCatalog
from core.ui_events import aread_session_revision
from sessions.executor.lock import background_in_flight, live_run_for, session_activity
from sessions.header import build_session_header, delegations_for, retry_for
from sessions.hydration import ahydrate_thread
from sessions.inbox.timeline import Timeline
from sessions.models import Run, RunStatus
from sessions.multi_repo import aload_extra_repos
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
    from sessions.models import Session, SessionRepository

logger = logging.getLogger("daiv.sessions")

MERGE_REQUEST_CACHE_TIMEOUT = 60

_HEADER_KEYS = ("latest_run", "run_count", "delegations", "ci", "watch", "up_next", "repositories")
_ROW_FIELDS = ["active_run_id", "last_active_at", "title", "ref"]


@dataclass(frozen=True)
class SessionState:
    """A session as ``user`` sees it on its page.

    ``runs`` are oldest first. ``live_run`` is the slot holder as the viewer sees it (``live_run_for``). ``header`` is
    :func:`~sessions.header.build_session_header` plus ``up_next`` and ``repositories``. ``expired_banner`` is whether
    the page says the session expired: no checkpoint or archive, nothing in flight, and no failed run whose prompt or
    marker ``annotate_transcript`` recovers. ``merge_request_known`` is false when the checkpoint has none and the
    platform was not asked or did not answer, so a ``None`` ``merge_request`` says nothing.
    """

    revision: int
    turns: list[dict[str, Any]]
    runs: list[Run]
    activity: SessionActivity
    hydrated: HydratedThread
    merge_request: dict | None
    merge_request_known: bool
    thread_ref: str
    live_run: LiveRun | None
    header: dict[str, Any]
    can_act: bool
    expired_banner: bool


async def aload_session_state(session: Session, user: User, *, always_look_up_merge_request: bool) -> SessionState:
    """``session``'s :class:`SessionState` for ``user``; ``always_look_up_merge_request`` asks the platform for a
    merge request the checkpoint lacks even while a run is in flight. Re-reads the fields of ``session`` that runs and
    renames change, so ``session`` comes back current."""
    revision = await aread_session_revision(session.thread_id)
    await session.arefresh_from_db(fields=_ROW_FIELDS)
    hydrated = await ahydrate_thread(session.thread_id)
    runs = [run async for run in Run.objects.filter(session=session).order_by("created_at")]
    activity = session_activity(session, runs)

    merge_request = hydrated.merge_request_payload
    look_up = always_look_up_merge_request or not activity.in_flight
    merge_request_known = look_up or merge_request is not None
    if merge_request is None and look_up and session.repo_id and session.ref:
        merge_request, merge_request_known = await _alook_up_merge_request(session, fresh=always_look_up_merge_request)

    annotated = annotate_transcript(build_turns(hydrated.messages, in_flight=activity.in_flight), runs, activity)
    expired_banner = hydrated.expired and not activity.in_flight and not annotated
    extra_repositories = await aload_extra_repos(session.thread_id)

    def finish() -> tuple[list[dict[str, Any]], dict[str, Any], bool, LiveRun | None]:
        turns = interleave_inbox_events(
            annotated if hydrated.messages else annotated + expired_run_turns(runs, annotated),
            runs,
            Timeline(session.thread_id).events(),
        )
        mark_run_openings(turns, Timeline(session.thread_id).opening_events())
        mark_skill_links(turns, session.thread_id)
        can_act = can_run(user, session.repo_id)
        runnable = {session.repo_id: can_act}
        for extra in extra_repositories:
            if extra.repo_id not in runnable:
                runnable[extra.repo_id] = can_act and can_run(user, extra.repo_id)
        delegations = delegations_for(turns, session, user)
        retry = retry_for(session, runs, turns, user, can_act=can_act)
        header = build_session_header(
            session, runs, retry=retry, user=user, delegations=delegations, merge_request=merge_request
        )
        header["up_next"] = up_next_payload(session, runs, user, activity)
        header["repositories"] = _repositories(
            session, runs, extra_repositories, main_diff_stats=hydrated.diff_stats, runnable=runnable
        )
        return turns, header, can_act, live_run_for(activity.live, session, user)

    turns, header, can_act, live_run = await sync_to_async(finish)()
    return SessionState(
        revision=revision,
        turns=turns,
        runs=runs,
        activity=activity,
        hydrated=hydrated,
        merge_request=merge_request,
        merge_request_known=merge_request_known,
        # The pill is the working branch, as the live stream's ``_applyRepoState`` couples it to the merge request;
        # ``Session.ref`` lags it on any run that never synced back.
        thread_ref=(merge_request or {}).get("source_branch") or session.ref,
        live_run=live_run,
        header=header,
        can_act=can_act,
        expired_banner=expired_banner,
    )


def _repositories(
    session: Session,
    runs: list[Run],
    extra_repositories: list[SessionRepository],
    *,
    main_diff_stats: dict | None,
    runnable: dict[str, bool],
) -> dict:
    """SYNC ONLY: the session's ``rows``, the main repository first, each with its branch, merge request, change
    size, whether the session ran and whether the viewer may act on its merge request (``runnable``), and the
    ``publish_errors`` of its latest run once it finished. The main row holds what the database and checkpoint know;
    the page lays the chat's live branch and merge request over it.

    A blank branch is the repository's default one, named when the catalog knows it. A change size that counts
    nothing is ``None``: a publish of a clean tree records zeros.
    """
    run_url = next((r.merge_request_web_url for r in reversed(runs) if r.merge_request_web_url), "")
    latest = runs[-1] if runs else None
    ran = any(r.status in RunStatus.terminal() for r in runs)
    rows = [
        {
            "repo_id": session.repo_id,
            "ref": session.ref,
            "mr_url": run_url,
            "mr_iid": session.merge_request_iid,
            "mr_title": "",
            "mr_draft": False,
            "diff_stats": _change_size(main_diff_stats),
            "ran": ran,
            "can_act": runnable[session.repo_id],
        },
        *(
            {
                "repo_id": r.repo_id,
                "ref": r.ref,
                "mr_url": r.merge_request_web_url,
                "mr_iid": r.merge_request_iid,
                "mr_title": r.merge_request_title,
                "mr_draft": r.merge_request_draft,
                "diff_stats": _change_size(r.diff_stats),
                "ran": ran,
                "can_act": runnable[r.repo_id],
            }
            for r in extra_repositories
        ),
    ]
    if unnamed := [row["repo_id"] for row in rows if not row["ref"]]:
        default_branches = dict(
            RepositoryCatalog.objects.filter(provider=codebase_settings.CLIENT.value, slug__in=unnamed).values_list(
                "slug", "default_branch"
            )
        )
        for row in rows:
            row["ref"] = row["ref"] or default_branches.get(row["repo_id"], "")
    return {
        "rows": rows,
        "publish_errors": latest.publish_errors if latest and latest.status in RunStatus.terminal() else [],
    }


def _change_size(diff_stats: dict | None) -> dict | None:
    if diff_stats and any(diff_stats.get(key) for key in ("lines_added", "lines_removed", "files_changed")):
        return diff_stats
    return None


async def _alook_up_merge_request(session: Session, *, fresh: bool) -> tuple[dict | None, bool]:
    """``session``'s open merge request on its branch, and whether the platform answered. A ``fresh`` lookup skips
    the cached answer; every answer is cached for :data:`MERGE_REQUEST_CACHE_TIMEOUT` seconds, a failure never."""
    key = f"sessions:merge-request:{session.thread_id}:{session.ref}"
    if not fresh and (cached := await cache.aget(key)) is not None:
        return cached["merge_request"], True
    try:
        merge_request = await aget_existing_mr_payload(session.repo_id, session.ref)
    except PLATFORM_ERRORS:
        logger.exception("Failed to look up the merge request of %s on %s", session.repo_id, session.ref)
        return None, False
    await cache.aset(key, {"merge_request": merge_request}, MERGE_REQUEST_CACHE_TIMEOUT)
    return merge_request, True


def turns_payload(state: SessionState, session: Session) -> dict[str, Any]:
    """The turns endpoint's body: what an open page applies to stay current without a reload.

    ``expired`` is the hydration's own flag; ``expired_banner`` is whether a render would show the expired banner,
    which a page that shows it reloads to leave. ``merge_request`` is left out while it is not known, so a page keeps
    the one it has.
    """
    activity, live = state.activity, state.live_run
    payload = {
        "revision": state.revision,
        "turns": state.turns,
        "active": activity.in_flight,
        "background": background_in_flight(activity, state.runs),
        "waiting_since": activity.waiting_since.isoformat() if activity.waiting_since else None,
        "expired": state.hydrated.expired,
        "expired_banner": state.expired_banner,
        "live_run": {"id": live.id, "stoppable": live.stoppable, "started_at": live.started_at.isoformat()}
        if live
        else None,
        **{key: state.header[key] for key in _HEADER_KEYS},
        "diff_stats": state.hydrated.diff_stats,
        "context_usage": state.hydrated.context_usage,
        "thread_ref": state.thread_ref,
        "title": session_title(session),
    }
    if state.merge_request_known:
        payload["merge_request"] = state.merge_request
    return payload
