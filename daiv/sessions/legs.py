"""A leg as its coordinator sees it: one piece of delegated work, a session whose ``parent`` is the coordinator, read
through its ``SessionOrigin.DELEGATED_JOB`` runs (its first run and any leg follow-ups' runs).

- **Where it started** (:attr:`Leg.base_ref`): the ``ref`` of its first delegated run, which a leg that waited for its
  coordinator was rebased onto (``sessions.queue``). Blank for the repository's default branch.
- **A branch of its own** (:meth:`Leg.has_own_branch`): the session works on a branch other than where it started.
  Started from the default branch, the session's ref is filled with that branch's name by a run that published
  nothing, so there it also takes a push by one of the session's runs.
- **Its merge request** (:meth:`Leg.merge_request`): the newest one its session's runs published on its main
  repository, open unless ``MergeRequestOwnership`` records it merged or closed (:meth:`Leg.merge_request_state`).
- **Busy** (:meth:`Leg.is_busy`): a delegated run on it has not ended: QUEUED (a leg follow-up waiting, or the leg
  waiting for its coordinator's turn), READY or RUNNING.
- **Lost** (:meth:`Leg.is_lost`): its latest delegated run reached the task queue but could not be linked to its row,
  so it may still be working while the row shows FAILED.
- **Archived** (:meth:`Leg.archived_by`): one person hid its session from their own list.

``sessions.queue`` imports this module, so it imports only ``sessions.models``. SYNC ONLY, but for the async wrappers.
A leg follow-up that does not run because its leg's merge request was merged or closed finishes its run FAILED with
:data:`NOT_RUN_MERGED` or :data:`NOT_RUN_CLOSED`, which readers of a run's outcome tell apart from a failure.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

from django.db.models import Exists, OuterRef
from django.utils.translation import gettext_noop

from asgiref.sync import sync_to_async

from sessions.models import (
    LINK_FAILED_PREFIX,
    EventKind,
    HiddenSession,
    InboxEvent,
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from accounts.models import User

NOT_RUN_MERGED = gettext_noop("Not run: its merge request was merged")
NOT_RUN_CLOSED = gettext_noop("Not run: its merge request was closed")
NOT_RUN_LABELS = frozenset({NOT_RUN_MERGED, NOT_RUN_CLOSED})


def is_leg_follow_up_run() -> Exists:
    """For a query on runs: whether the run is a leg follow-up's, whose ``leg_follow_up`` event is linked to it."""
    return Exists(InboxEvent.objects.filter(kind=EventKind.LEG_FOLLOW_UP, run=OuterRef("pk")))


@dataclass(frozen=True)
class LegMergeRequest:
    repo_id: str
    iid: int
    web_url: str


class Leg:
    """The leg whose session is ``session``."""

    def __init__(self, session: Session):
        self.session = session
        self._pushed: bool | None = None
        self._published: list[tuple[int, str]] | None = None

    @classmethod
    def many(cls, sessions: Iterable[Session]) -> dict[str, Leg]:
        """A leg for each of ``sessions``, by thread id, with what :attr:`base_ref`, :meth:`has_own_branch` and
        :meth:`merge_request` read loaded in three queries for all of them. SYNC ONLY."""
        legs: dict[str, Leg] = {session.pk: cls(session) for session in sessions}
        runs = Run.objects.filter(session_id__in=list(legs))
        first_refs: dict[str, str] = {}
        delegated = runs.filter(trigger_type=SessionOrigin.DELEGATED_JOB).order_by("created_at", "id")
        for thread_id, ref in delegated.values_list("session_id", "ref"):
            first_refs.setdefault(thread_id, ref)
        pushed = set(runs.that_pushed().values_list("session_id", flat=True))
        published: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
        rows = (
            runs
            .filter(merge_request_iid__isnull=False)
            .order_by("-created_at", "-id")
            .values_list("session_id", "repo_id", "merge_request_iid", "merge_request_web_url")
        )
        for thread_id, repo_id, iid, web_url in rows:
            if repo_id == legs[thread_id].session.repo_id:
                published[thread_id].append((iid, web_url))
        for thread_id, leg in legs.items():
            leg.base_ref = first_refs.get(thread_id, leg.session.ref)
            leg._pushed = thread_id in pushed
            leg._published = published[thread_id]
        return legs

    @classmethod
    def of(cls, thread_id: str, *, coordinator: str) -> Leg | None:
        """The leg ``thread_id``, or ``None`` unless it is a session ``coordinator`` delegated work to."""
        session = Session.objects.filter(pk=thread_id, parent_id=coordinator).first()
        if session is None:
            return None
        leg = cls(session)
        return leg if leg.delegated_runs().exists() else None

    @classmethod
    async def aof(cls, thread_id: str, *, coordinator: str) -> Leg | None:
        return await sync_to_async(cls.of)(thread_id, coordinator=coordinator)

    def delegated_runs(self) -> QuerySet[Run]:
        """Its delegated runs, oldest first."""
        return Run.objects.filter(session=self.session, trigger_type=SessionOrigin.DELEGATED_JOB).order_by(
            "created_at", "id"
        )

    @cached_property
    def base_ref(self) -> str:
        first = self.delegated_runs().values_list("ref", flat=True).first()
        return self.session.ref if first is None else first

    def has_own_branch(self) -> bool:
        ref = self.session.ref
        if not ref or ref == self.base_ref:
            return False
        if self.base_ref:
            return True
        if self._pushed is not None:
            return self._pushed
        return Run.objects.filter(session=self.session).that_pushed().exists()

    async def ahas_own_branch(self) -> bool:
        return await sync_to_async(self.has_own_branch)()

    def merge_request(self) -> LegMergeRequest | None:
        published = self._published
        if published is None:
            published = list(
                Run.objects
                .filter(session=self.session, repo_id=self.session.repo_id, merge_request_iid__isnull=False)
                .order_by("-created_at", "-id")
                .values_list("merge_request_iid", "merge_request_web_url")
            )
        if not published:
            return None
        iid = published[0][0]
        web_url = next((url for run_iid, url in published if run_iid == iid and url), "")
        return LegMergeRequest(self.session.repo_id, iid, web_url)

    def merge_request_state(self) -> MergeRequestState:
        if (merge_request := self.merge_request()) is None:
            return MergeRequestState.OPEN
        key = (merge_request.repo_id, merge_request.iid)
        return MergeRequestOwnership.states_of([key])[key]

    def is_busy(self) -> bool:
        return self.delegated_runs().exclude(status__in=RunStatus.terminal()).exists()

    def is_lost(self) -> bool:
        latest = self.delegated_runs().reverse().values_list("status", "error_message").first()
        return latest is not None and latest[0] == RunStatus.FAILED and latest[1].startswith(LINK_FAILED_PREFIX)

    def archived_by(self, user: User) -> bool:
        return HiddenSession.objects.filter(session=self.session, user=user).exists()
