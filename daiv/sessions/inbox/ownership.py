"""Which session owns a merge request DAIV published (``MergeRequestOwnership``, FR-001).

A run that publishes claims the merge request (:func:`aclaim_ownership`), after which CI results, mentions, pushes
and merges on it reach the owning session's inbox. The claim rules:

| Current state | Publishing session | Result |
|---|---|---|
| No row | any | the row is created, owned by the publishing session and its user |
| Owned by the same person, or both ownerless | any session of that person | ownership moves to the publishing session |
| The owner has no user | a session with a user | ownership moves; the old session gets a ``notice`` |
| Owned by person A | a session of person B | ownership stays; A's session gets a ``foreign_publish`` |

An owning session that was deleted counts as no row for its own person; a new session is made for that person when
an event needs one (:func:`aowning_session`). Every claim records the commit the run pushed as ``last_daiv_sha``,
which is how the CI watch tells DAIV's commits from everyone else's, and a claim by a run a person started gives the
CI watch its attempts back.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from django.db import transaction

from asgiref.sync import sync_to_async

from sessions.inbox import kinds
from sessions.inbox.post import apost_event
from sessions.models import EventKind, MergeRequestOwnership, Run, Session, SessionOrigin
from sessions.repo_state import mr_to_payload
from sessions.titling import MAX_TITLE_LENGTH

if TYPE_CHECKING:
    from codebase.base import MergeRequest

logger = logging.getLogger("daiv.sessions")

#: Triggers of runs a person started, whose publish gives the CI watch its attempts back (FR-016).
PERSON_STARTED = frozenset({
    SessionOrigin.CHAT,
    SessionOrigin.MR_WEBHOOK,
    SessionOrigin.API_JOB,
    SessionOrigin.MCP_JOB,
    SessionOrigin.UI_JOB,
    SessionOrigin.ISSUE_WEBHOOK,
    SessionOrigin.SCHEDULE,
})


async def aclaim_ownership(
    *, run_id: uuid.UUID | str, merge_request: MergeRequest | dict | None, pushed_sha: str
) -> MergeRequestOwnership | None:
    """Apply the claim rules for the merge request the run ``run_id`` published, which pushed ``pushed_sha``;
    ``None`` when the merge request is unknown."""
    payload = mr_to_payload(merge_request) or {}
    iid, branch = payload.get("id"), payload.get("source_branch")
    if not iid or not branch:
        return None
    ownership, posts = await sync_to_async(_claim)(run_id, iid, branch, pushed_sha)
    for post in posts:
        try:
            await apost_event(**post)
        except Exception:
            logger.exception(
                "inbox: could not tell thread_id=%s about %s!%s", post["thread_id"], ownership.repo_id, iid
            )
    return ownership


def _claim(run_id: uuid.UUID | str, iid: int, branch: str, pushed_sha: str) -> tuple[MergeRequestOwnership, list[dict]]:
    run = Run.objects.select_related("session__user").get(pk=run_id)
    session = run.session
    posts: list[dict[str, Any]] = []
    with transaction.atomic():
        row = (
            MergeRequestOwnership.objects
            .select_for_update()
            .select_related("session")
            .filter(repo_id=run.repo_id, merge_request_iid=iid)
            .first()
        )
        if row is None:
            row = MergeRequestOwnership.objects.create(
                repo_id=run.repo_id,
                merge_request_iid=iid,
                session=session,
                owner=session.user,
                source_branch=branch,
                last_daiv_sha=pushed_sha,
            )
            return row, posts

        previous = row.session
        if row.owner_id == session.user_id:
            row.session = session
        elif row.owner_id is None:
            row.session, row.owner = session, session.user
            if previous is not None and previous.pk != session.pk:
                posts.append(_moved_notice(previous, session, iid, branch, run_id))
        elif previous is not None:
            posts.append(_foreign_publish(previous, session, iid, branch, pushed_sha, run_id))
        else:
            logger.info(
                "inbox: %s!%s belongs to a deleted session of another person; thread_id=%s published to it",
                run.repo_id,
                iid,
                session.thread_id,
            )
        row.source_branch, row.last_daiv_sha = branch, pushed_sha
        if run.trigger_type in PERSON_STARTED:
            row.attempts = 0
        row.save()
    return row, posts


def _username(session: Session) -> str:
    return session.user.username if session.user is not None else session.external_username or "someone"


def _moved_notice(
    previous: Session, session: Session, iid: int, branch: str, run_id: uuid.UUID | str
) -> dict[str, Any]:
    return {
        "thread_id": previous.thread_id,
        "kind": EventKind.NOTICE,
        "source_key": f"ownership-moved:{iid}:{run_id}",
        "payload": {
            "text": (
                f"!{iid} is now owned by a session of {_username(session)}, which published to it; CI results and "
                "mentions on it go there from now on."
            )
        },
        "wake": False,
        "ref": branch,
        "merge_request_iid": iid,
    }


def _foreign_publish(
    owner: Session, session: Session, iid: int, branch: str, pushed_sha: str, run_id: uuid.UUID | str
) -> dict[str, Any]:
    return {
        "thread_id": owner.thread_id,
        "kind": EventKind.FOREIGN_PUBLISH,
        "source_key": f"foreign-publish:{pushed_sha or run_id}",
        "payload": {"thread_id": session.thread_id, "owner_username": _username(session), "sha": pushed_sha},
        "wake": False,
        "subject": kinds.branch_subject(branch),
        "ref": branch,
        "merge_request_iid": iid,
    }


def owning_session(repo_id: str, merge_request_iid: int, *, title: str = "") -> Session | None:
    """The session that owns the open merge request, or ``None`` when no session does or it is closed. SYNC ONLY.

    When the owning session was deleted, a new one is made for the same person, named ``title``.
    """
    with transaction.atomic():
        row = (
            MergeRequestOwnership.objects
            .select_for_update()
            .select_related("session")
            .filter(repo_id=repo_id, merge_request_iid=merge_request_iid, closed_at__isnull=True)
            .first()
        )
        if row is None:
            return None
        if row.session is None:
            row.session = Session.objects.create(
                thread_id=str(uuid.uuid4()),
                origin=SessionOrigin.PIPELINE_WEBHOOK,
                repo_id=repo_id,
                ref=row.source_branch,
                user_id=row.owner_id,
                title=(title or f"!{merge_request_iid}")[:MAX_TITLE_LENGTH],
                merge_request_iid=merge_request_iid,
            )
            row.save(update_fields=["session", "updated_at"])
        return row.session


aowning_session = sync_to_async(owning_session)


async def aownership_for_branch(repo_id: str, source_branch: str) -> MergeRequestOwnership | None:
    """The open ownership of the merge request whose source branch is ``source_branch``."""
    return await (
        MergeRequestOwnership.objects
        .filter(repo_id=repo_id, source_branch=source_branch, closed_at__isnull=True)
        .order_by("-updated_at")
        .afirst()
    )
