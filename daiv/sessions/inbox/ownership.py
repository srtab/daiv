"""Which session owns a merge request DAIV published (``MergeRequestOwnership``, FR-001).

A run that publishes claims the merge request (:meth:`Ownerships.aclaim`), after which CI results, mentions, pushes
and merges on it reach the owning session's inbox. The claim rules:

| Current state | Publishing session | Result |
|---|---|---|
| No row | any | the row is created, owned by the publishing session and its user |
| Owned by the same person, or both ownerless | any session of that person | ownership moves to the publishing session |
| The owner has no user | a session with a user | ownership moves; the old session gets a ``notice`` |
| Owned by person A | a session of person B | ownership stays; A's session gets a ``foreign_publish`` naming B's |

An owning session that was deleted counts as no row for its own person; a new session is made for that person when
an event needs one (:meth:`Ownerships.find_open`) or another person publishes, and its first event is a ``notice``
saying why it exists. Every claim records the commit the run pushed as ``last_daiv_sha``, which is how the CI watch
tells DAIV's commits from everyone else's, and a claim by a run a person started gives the CI watch its attempts back.

What else happens to an owned merge request reaches the owning session as remembered events: a push to its branch
whose commit DAIV did not push (``branch_pushed``, decided ``PUSH_CLASSIFY_DELAY`` after the webhook so DAIV's own
publish has recorded its commit by then), its merge or close (``mr_merged``, ``mr_closed``; the watch stops), and a
reopen (a ``notice``; the watch resumes). A close and a reopen are each keyed on when they happened, so a merge request
closed again after a reopen is remembered again; a redelivered close finds it closed already and posts nothing. A close
or reopen whose event could not be posted is undone, unless another was recorded meanwhile, so a redelivery posts it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from asgiref.sync import sync_to_async

from core.utils import build_absolute_url
from sessions.inbox import kinds
from sessions.inbox.post import Inbox
from sessions.models import EventKind, MergeRequestOwnership, Run, Session, SessionOrigin
from sessions.repo_state import mr_to_payload
from sessions.templatetags.session_tags import session_title
from sessions.titling import MAX_TITLE_LENGTH

if TYPE_CHECKING:
    from codebase.base import MergeRequest

logger = logging.getLogger("daiv.sessions")

PUSH_CLASSIFY_DELAY = timedelta(seconds=60)

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


class Ownerships:
    """The merge requests a repository's sessions own."""

    def __init__(self, repo_id: str):
        self.repo_id = repo_id

    @classmethod
    async def aclaim(
        cls, *, run_id: uuid.UUID | str, merge_request: MergeRequest | dict | None, pushed_sha: str
    ) -> MergeRequestOwnership | None:
        """Apply the claim rules for the merge request the run ``run_id`` published, which pushed ``pushed_sha``;
        ``None`` when the merge request is unknown."""
        payload = mr_to_payload(merge_request) or {}
        iid, branch = payload.get("id"), payload.get("source_branch")
        if not iid or not branch:
            return None
        ownership, posts = await sync_to_async(cls._claim_for_run)(
            run_id, iid, branch, pushed_sha, payload.get("title") or ""
        )
        ownerships = cls(ownership.repo_id)
        for post in posts:
            await ownerships._apost_logged(post)
        return ownership

    @classmethod
    def _claim_for_run(
        cls, run_id: uuid.UUID | str, iid: int, branch: str, pushed_sha: str, title: str
    ) -> tuple[MergeRequestOwnership, list[dict]]:
        run = Run.objects.select_related("session__user").get(pk=run_id)
        return cls(run.repo_id)._claim(run, iid, branch, pushed_sha, title)

    def _post_logged(self, post: dict[str, Any]) -> None:
        """Post ``post``, logging a failure instead of raising: the ownership change it reports has committed, and the
        event wakes no one. SYNC ONLY."""
        try:
            thread_id = post["thread_id"]
            Inbox(thread_id).post(**{k: v for k, v in post.items() if k != "thread_id"})
        except Exception:
            logger.exception(
                "inbox: could not tell thread_id=%s about %s!%s",
                post["thread_id"],
                self.repo_id,
                post["merge_request_iid"],
            )

    async def _apost_logged(self, post: dict[str, Any]) -> None:
        return await sync_to_async(self._post_logged)(post)

    def _claim(
        self, run: Run, iid: int, branch: str, pushed_sha: str, title: str
    ) -> tuple[MergeRequestOwnership, list[dict]]:
        session = run.session
        posts: list[dict[str, Any]] = []
        with transaction.atomic():
            row = (
                MergeRequestOwnership.objects
                .select_for_update(of=("self",))
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
                    posts.append(self._moved_notice(previous, session, iid, branch, run.pk))
            else:
                owner = previous if previous is not None else self._replace_session(row, title=title)
                posts.append(self._foreign_publish(owner, session, iid, branch, pushed_sha, run.pk))
            if row.last_daiv_sha != pushed_sha:
                # A pipeline judged before this claim was judged against the previous commit: judge it anew.
                row.last_pipeline_id, row.last_pipeline_status = None, ""
            row.source_branch, row.last_daiv_sha = branch, pushed_sha
            if run.trigger_type in PERSON_STARTED:
                row.attempts = 0
            row.save()
        return row, posts

    @staticmethod
    def _username(session: Session) -> str:
        return session.user.username if session.user is not None else session.external_username or "someone"

    def _moved_notice(
        self, previous: Session, session: Session, iid: int, branch: str, run_id: uuid.UUID | str
    ) -> dict[str, Any]:
        return {
            "thread_id": previous.thread_id,
            "kind": EventKind.NOTICE,
            "source_key": f"ownership-moved:{iid}:{run_id}",
            "payload": {
                "text": (
                    f"!{iid} is now owned by a session of {self._username(session)}, which published to it; CI "
                    "results and mentions on it go there from now on."
                )
            },
            "wake": False,
            "ref": branch,
            "merge_request_iid": iid,
        }

    def _foreign_publish(
        self, owner: Session, session: Session, iid: int, branch: str, pushed_sha: str, run_id: uuid.UUID | str
    ) -> dict[str, Any]:
        return {
            "thread_id": owner.thread_id,
            "kind": EventKind.FOREIGN_PUBLISH,
            "source_key": f"foreign-publish:{pushed_sha or run_id}",
            "payload": {
                "thread_id": session.thread_id,
                "owner_username": self._username(session),
                "sha": pushed_sha,
                "title": session_title(session),
                "web_url": build_absolute_url(reverse("session_detail", kwargs={"thread_id": session.thread_id})),
            },
            "wake": False,
            "subject": kinds.Subject.branch(branch),
            "ref": branch,
            "merge_request_iid": iid,
        }

    def find_open(self, merge_request_iid: int, *, title: str = "") -> MergeRequestOwnership | None:
        """The open merge request's ownership, whose ``session`` owns it, or ``None`` when no session does or it is
        closed. SYNC ONLY.

        When the owning session was deleted, a new one is made for the same person, named ``title``
        (:meth:`_replace_session`).
        """
        with transaction.atomic():
            row = (
                MergeRequestOwnership.objects
                .select_for_update(of=("self",))
                .select_related("session")
                .filter(repo_id=self.repo_id, merge_request_iid=merge_request_iid, closed_at__isnull=True)
                .first()
            )
            if row is None:
                return None
            if row.session is None:
                self._replace_session(row, title=title)
                row.save(update_fields=["session", "updated_at"])
            return row

    async def afind_open(self, merge_request_iid: int, *, title: str = "") -> MergeRequestOwnership | None:
        return await sync_to_async(self.find_open)(merge_request_iid, title=title)

    def _replace_session(self, row: MergeRequestOwnership, *, title: str) -> Session:
        """A new session owning ``row`` for its owner, whose owning session was deleted, named ``title``; a ``notice``
        tells it why it exists once it commits. The caller saves ``row``. SYNC ONLY, in the transaction that locks
        ``row``.
        """
        iid = row.merge_request_iid
        row.session = Session.objects.create(
            thread_id=str(uuid.uuid4()),
            origin=SessionOrigin.PIPELINE_WEBHOOK,
            repo_id=row.repo_id,
            ref=row.source_branch,
            user_id=row.owner_id,
            title=(title or f"!{iid}")[:MAX_TITLE_LENGTH],
            merge_request_iid=iid,
        )
        notice = {
            "thread_id": row.session.thread_id,
            "kind": EventKind.NOTICE,
            "source_key": f"ownership-replaced:{iid}",
            "payload": {
                "text": (
                    f"The session that owned !{iid} was deleted, so this one was made for it; CI results and "
                    f"mentions on !{iid} come here from now on."
                )
            },
            "wake": False,
            "ref": row.source_branch,
            "merge_request_iid": iid,
        }
        transaction.on_commit(partial(self._post_logged, notice))
        return row.session

    async def afor_branch(self, source_branch: str) -> MergeRequestOwnership | None:
        """The open ownership of the merge request whose source branch is ``source_branch``."""
        return await (
            MergeRequestOwnership.objects
            .filter(repo_id=self.repo_id, source_branch=source_branch, closed_at__isnull=True)
            .order_by("-updated_at")
            .afirst()
        )

    async def aschedule_push_classification(
        self, *, branch: str, before: str, after: str, pusher_username: str, commit_count: int
    ) -> bool:
        """Queue the decision on a push to ``branch`` for ``PUSH_CLASSIFY_DELAY`` from now, when a session owns an
        open merge request on it; returns whether one was queued."""
        from sessions.tasks import classify_push_task

        if await self.afor_branch(branch) is None:
            return False
        await classify_push_task.using(run_after=timezone.now() + PUSH_CLASSIFY_DELAY).aenqueue(
            repo_id=self.repo_id,
            branch=branch,
            before=before,
            after=after,
            pusher_username=pusher_username,
            commit_count=commit_count,
        )
        return True

    async def aclassify_push(
        self, *, branch: str, before: str, after: str, pusher_username: str, commit_count: int
    ) -> None:
        """Remember a push to an owned branch whose commit DAIV did not push, for the owning session's next run."""
        ownership = await self.afor_branch(branch)
        if ownership is None or after in (ownership.last_daiv_sha, ""):
            return
        if await Run.objects.filter(repo_id=self.repo_id, pushed_sha=after).aexists():
            return
        if (owned := await self.afind_open(ownership.merge_request_iid)) is None:
            return
        await Inbox(owned.session.thread_id).apost(
            kind=EventKind.BRANCH_PUSHED,
            source_key=kinds.SourceKey.push(after),
            payload={
                "before": before,
                "after": after,
                "commit_count": commit_count,
                "pusher_username": pusher_username,
            },
            wake=False,
            subject=kinds.Subject.branch(branch),
            ref=branch,
            merge_request_iid=ownership.merge_request_iid,
        )

    async def aclose(self, merge_request_iid: int, *, merged: bool, by_username: str, target_branch: str) -> bool:
        """Stop the watch on a merged or closed merge request and remember it in the owning session; returns whether
        this call closed one a session owned. A redelivered close, even one racing the first, finds it closed and
        posts nothing.
        """
        if (owned := await self.afind_open(merge_request_iid)) is None:
            return False
        closed_at = timezone.now()
        if not await MergeRequestOwnership.objects.filter(pk=owned.pk, closed_at__isnull=True).aupdate(
            closed_at=closed_at
        ):
            return False
        state = "merged" if merged else "closed"
        try:
            await Inbox(owned.session.thread_id).apost(
                kind=EventKind.MR_MERGED if merged else EventKind.MR_CLOSED,
                source_key=f"{kinds.SourceKey.mr_state(merge_request_iid, state)}:{closed_at.timestamp():.6f}",
                payload={"target_branch": target_branch, "by_username": by_username},
                wake=False,
                subject=kinds.Subject.mr_state(merge_request_iid),
                merge_request_iid=merge_request_iid,
            )
        except Exception:
            await MergeRequestOwnership.objects.filter(pk=owned.pk, closed_at=closed_at).aupdate(closed_at=None)
            raise
        return True

    async def areopen(self, merge_request_iid: int, *, by_username: str) -> bool:
        """Watch a reopened merge request again and tell the owning session; returns whether it was closed and owned."""
        closed = await (
            MergeRequestOwnership.objects
            .filter(repo_id=self.repo_id, merge_request_iid=merge_request_iid, closed_at__isnull=False)
            .only("pk", "closed_at")
            .afirst()
        )
        if closed is None or not await MergeRequestOwnership.objects.filter(
            pk=closed.pk, closed_at=closed.closed_at
        ).aupdate(closed_at=None):
            return False
        try:
            if (owned := await self.afind_open(merge_request_iid)) is None:
                return False
            await Inbox(owned.session.thread_id).apost(
                kind=EventKind.NOTICE,
                source_key=f"mr-reopened:{merge_request_iid}:{timezone.now().timestamp():.0f}",
                payload={
                    "text": f"!{merge_request_iid} was reopened by {by_username}; its CI and mentions come here again."
                },
                wake=False,
                subject=kinds.Subject.mr_state(merge_request_iid),
                merge_request_iid=merge_request_iid,
            )
        except Exception:
            await MergeRequestOwnership.objects.filter(pk=closed.pk, closed_at__isnull=True).aupdate(
                closed_at=closed.closed_at
            )
            raise
        return True
