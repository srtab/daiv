"""What the session page's top bar and details panel show beyond the session's own columns: the latest run's status
and what its Retry sends, the CI watch on its merge request, and the issue an issue-webhook session addresses.

``latest_run_payload`` and ``watch_payload`` are also what the turns poll returns, so what the page rendered and what
keeps it current are built by the same code.

The CI watch lives on the merge request's own thread, which a chat or job session that published the merge request
is not, so ``watch_state`` reads it off that row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.urls import reverse

from codebase.authorization import can_run
from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryCatalog
from sessions.delegation import FAILED, NEEDS_YOU, RUNNING, WAITING
from sessions.models import Run, Session, SessionOrigin, WatchState
from sessions.pipeline_watch.store import WatchStore, watch_thread_id
from sessions.templatetags.session_tags import session_title, status_variant
from sessions.transcript import marker_id, retry_message

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping

    from accounts.models import User
    from sessions.delegation import Delegations

GOAL_MAX_CHARS = 120

_WATCH_TONES = {
    WatchState.WATCHING: "attn",
    WatchState.FIXING: "attn",
    WatchState.GREEN: "clear",
    WatchState.EXHAUSTED: "fail",
    WatchState.UNCLEAR: "found",
}


def retry_for(
    session: Session, runs: list[Run], turns: list[dict[str, Any]], user: User, *, can_act: bool | None = None
) -> str:
    """SYNC ONLY: ``retry_message`` for a ``user`` the chat endpoint lets post to ``session``, else empty.

    ``can_act`` is ``can_run`` on the session's repository, for a caller that already has it.
    """
    if not (message := retry_message(turns, runs)):
        return ""
    if can_act is None:
        can_act = can_run(user, session.repo_id)
    if not can_act or not Session.objects.by_owner(user).filter(pk=session.pk).exists():
        return ""
    return message


def latest_run_payload(runs: list[Run], retry: str) -> dict[str, str] | None:
    """The newest of ``runs`` (oldest first) as the header's status pill and the Retry on its marker read it.

    ``retry`` is what ``retry_for`` returned for the same runs.
    """
    if not runs:
        return None
    latest = runs[-1]
    return {
        "marker_id": marker_id(latest),
        "label": str(latest.get_status_display()),
        "variant": status_variant(latest.status),
        "finished_at": latest.finished_at.isoformat() if latest.finished_at else "",
        "retry": retry,
    }


def merge_request_iid(session: Session, runs: list[Run], merge_request: dict | None) -> int | None:
    """The merge request the page shows: the checkpoint's, else the session's own, else the newest run's."""
    if iid := (merge_request or {}).get("id"):
        return iid
    if session.merge_request_iid:
        return session.merge_request_iid
    return next((r.merge_request_iid for r in reversed(runs) if r.merge_request_iid), None)


def _other_watch_thread(session: Session, iid: int | None) -> str | None:
    """The thread holding the CI watch on merge request ``iid``, or ``None`` when ``session`` holds it itself."""
    if not iid or not session.repo_id:
        return None
    thread_id = watch_thread_id(session.repo_id, iid)
    return None if thread_id == session.thread_id else thread_id


def watch_state(session: Session, iid: int | None) -> str | None:
    """SYNC ONLY: the state of the CI watch on merge request ``iid``; ``None`` while nothing ever armed one."""
    thread_id = _other_watch_thread(session, iid)
    return session.watch_state if thread_id is None else WatchStore().state(thread_id)


async def awatch_state(session: Session, iid: int | None) -> str | None:
    """:func:`watch_state` for async callers."""
    thread_id = _other_watch_thread(session, iid)
    return session.watch_state if thread_id is None else await WatchStore().astate(thread_id)


def watch_payload(state: str | None) -> dict[str, Any] | None:
    """The CI watch in ``state``, or ``None`` when nothing is watched; ``open`` while it still waits on or fixes CI."""
    tone = _WATCH_TONES.get(state)
    if tone is None:
        return None
    return {"state": state, "label": str(WatchState(state).label), "tone": tone, "open": state in WatchState.open()}


def issue_link(session: Session, runs: list[Run]) -> dict[str, Any] | None:
    """SYNC ONLY: the issue an issue-webhook session addresses, read off the task that started its first issue run.

    The thread id is a hash of the issue, so the task's ``issue_iid`` kwarg is the only record of which one it was.
    ``url`` is blank when the repository is not in the catalog, which is where the platform's web URL comes from.
    """
    if session.origin != SessionOrigin.ISSUE_WEBHOOK:
        return None
    run = next((r for r in runs if r.trigger_type == SessionOrigin.ISSUE_WEBHOOK and r.task_result_id), None)
    if run is None:
        return None
    iid = ((run.task_result.args_kwargs or {}).get("kwargs") or {}).get("issue_iid")
    if not iid:
        return None
    html_url = (
        RepositoryCatalog.objects
        .filter(provider=codebase_settings.CLIENT.value, slug=session.repo_id)
        .values_list("html_url", flat=True)
        .first()
    )
    if not html_url:
        return {"iid": iid, "url": ""}
    path = "-/issues" if codebase_settings.CLIENT == GitPlatform.GITLAB else "issues"
    return {"iid": iid, "url": f"{html_url.rstrip('/')}/{path}/{iid}"}


def delegations_payload(delegations: Delegations, goals: Mapping[uuid.UUID, str]) -> dict[str, Any] | None:
    """The jobs a session delegated, newest batch first, or ``None`` when it never delegated.

    A batch's ``goal`` is the one in ``goals``, else the first line of the prompt of its first leg the viewer may
    see. ``legs`` holds every batch's legs in one list, those in ``NEEDS_YOU`` first.
    """
    if not delegations.batches:
        return None
    newest_first = sorted(delegations.batches.items(), key=lambda item: item[1][0].created_at, reverse=True)
    batches = [
        {
            "goal": goals.get(batch_id) or _prompt_goal(legs, delegations.visible),
            "created_at": legs[0].created_at.isoformat(),
            "legs": delegations.legs[batch_id],
        }
        for batch_id, legs in newest_first
    ]
    legs = sorted((leg for batch in batches for leg in batch["legs"]), key=lambda leg: leg["state"] not in NEEDS_YOU)
    states = [leg["state"] for leg in legs]
    return {
        "total": len(states),
        "running": sum(state in RUNNING for state in states),
        "waiting": sum(state in WAITING for state in states),
        "need_you": sum(state in NEEDS_YOU for state in states),
        "failed": sum(state in FAILED for state in states),
        "batches": batches,
        "legs": legs,
    }


def _prompt_goal(legs: list[Run], visible: frozenset[str]) -> str:
    first = next((leg for leg in legs if leg.session_id in visible), None)
    if first is None:
        return ""
    return (first.prompt.strip().splitlines() or [""])[0][:GOAL_MAX_CHARS]


def parent_link(session: Session, user: User) -> dict[str, str] | None:
    """SYNC ONLY: the session that delegated ``session``, when ``user`` may see it."""
    if not session.parent_id:
        return None
    parent = Session.objects.visible_to(user).filter(pk=session.parent_id).first()
    if parent is None:
        return None
    return {"title": session_title(parent), "url": reverse("session_detail", kwargs={"thread_id": parent.thread_id})}


def build_session_header(
    session: Session,
    runs: list[Run],
    *,
    retry: str,
    user: User,
    delegations: Delegations,
    goals: Mapping[uuid.UUID, str],
    merge_request: dict | None = None,
) -> dict[str, Any]:
    """SYNC ONLY: the header's context, for ``runs`` ordered oldest first, the ``delegations`` the transcript shows
    with their ``goals``, and the ``merge_request`` the page shows."""
    return {
        "latest_run": latest_run_payload(runs, retry),
        "watch": watch_payload(watch_state(session, merge_request_iid(session, runs, merge_request))),
        "issue": issue_link(session, runs),
        "run_count": len(runs),
        "delegations": delegations_payload(delegations, goals),
        "parent": parent_link(session, user),
    }
