"""Starting a session's next run from its pending events (``specs/001-session-inbox/contracts/inbox-service.md``).

:meth:`Drain.start` locks the session row and, unless the session is archived, a live holder has the slot or a job run
is active, takes its pending events in one transaction: it moves the requests that follow the session to its branch,
supersedes all but the newest event per subject, turns the CI and delegation wakes the policy blocks into remembered
events, and drops a QUEUED next run left with nothing to wake it. When a waking event survives, it picks the branch and
requester of the oldest one (an event about another of the session's repositories is on the session's branch,
:meth:`sessions.inbox.next_runs.NextRuns.branch_of`), checks that requester may still run on the repository and read the
submodules DAIV fetched in the session (a mention is gated where it is made, FR-026), and takes their waking events for
the branch with every remembered one, up to their first request that runs alone
(:meth:`sessions.inbox.splits.RequestSplits.runs_alone`). A builtin slash command that
comes first is taken alone, with nothing remembered: the slash-command middleware answers it only as the whole request,
and ends the run. A leg follow-up or a merge request job that comes first is taken alone with every remembered event,
unless its request is a builtin slash command, and its own run, posted with it, is promoted to READY to run it; when its
merge request was merged or closed meanwhile it is stopped instead, its run finishing FAILED never started
(:meth:`sessions.inbox.own_runs.OwnRunEvents.stop`), and the drain runs again once that commits.
Otherwise the requester's next run for the branch is promoted to READY to run what was taken, unless requests it holds
stay behind; it then keeps waiting for them, and a READY run is created instead. The drain claims the slot for that run
and marks its events ``taken``. A CI failure among them spends one of its merge request's attempts on the commit it
reports in that same transaction, so a crash before the enqueue still counts it. A denied requester's events fail and
their next run ends FAILED with the ``access_denied`` label; their leg follow-ups and merge request jobs are stopped
with it, a job saying so on its merge request (a job's id still resolves, a leg follow-up's batch finishes), and the
drain goes on to the next requester. A merge request job only attributed to its requester, a CI identity's, needs no
access to the repository the platform proved, but its linked user must be able to run on the session's others and read
its submodules, and with none there must be none; it neither counts toward nor fails with that user's own requests.
The run is enqueued once the take commits; a failed enqueue frees the slot, puts the events back and refunds the
attempt, for the sweep to retry.

A next run is a QUEUED run with an inbox trigger type, no task yet, and events linked to it. ``sessions.queue`` leaves
such runs alone: only this module starts them, because only it claims the slot for them.
"""

from __future__ import annotations

import logging
import uuid
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, NamedTuple

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.tasks import TaskResultStatus
from django.utils import timezone

from asgiref.sync import async_to_sync, sync_to_async
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.selection import resolve_env_for_run

from automation.agent.references import merge_stored_refs
from codebase.authorization import viewable_repo_ids
from sessions.admission import RepoAccessError, assert_repo_access
from sessions.executor.lock import SessionLock, stale_cutoff
from sessions.inbox.kinds import OWN_RUN_KINDS, PERSON_KINDS, PRECEDENCE, EventContent
from sessions.inbox.leg_follow_ups import LegFollowUps
from sessions.inbox.merge_request_jobs import MergeRequestJobs
from sessions.inbox.next_runs import NextRuns
from sessions.inbox.pending import PendingEvents
from sessions.inbox.policy import WakePolicy
from sessions.inbox.render import RunInput
from sessions.inbox.requester import Requester
from sessions.inbox.splits import RequestSplits
from sessions.live.notify import session_changed
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    QuietReason,
    Run,
    RunStatus,
    Session,
)
from sessions.queue import aenqueue_run
from sessions.services import pinned_agent_override

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.inbox.own_runs import OwnRunEvents

logger = logging.getLogger("daiv.sessions")

DRAIN_SWEEP_LIMIT = 200

OWN_RUNS: dict[str, type[OwnRunEvents]] = {own_runs.kind: own_runs for own_runs in (LegFollowUps, MergeRequestJobs)}


class Drain:
    """The drain of the session ``thread_id``: starting its next run from its pending events."""

    def __init__(self, thread_id: str):
        self.thread_id = thread_id

    def start(self) -> Run | None:
        """Start the session's next run from its pending events, and return it; ``None`` when none started.
        SYNC ONLY."""
        try:
            took = self._take()
        except Session.DoesNotExist:
            return None
        if took is None:
            return None
        run, charges = took
        if async_to_sync(aenqueue_run)(run, log_context="inbox", skip_dispatch=True, inbox=True) is not None:
            logger.error(
                "inbox: could not enqueue run=%s for thread_id=%s; its events wait for the next drain",
                run.pk,
                self.thread_id,
            )
            self._undo_take(run, charges)
            return None
        return run

    async def astart(self) -> Run | None:
        return await sync_to_async(self.start)()

    def start_logged(self) -> None:
        """:meth:`start` for callers that must not fail with it (commit hooks, signal receivers, sweeps). SYNC ONLY."""
        try:
            self.start()
        except Exception:
            logger.exception("inbox: draining thread_id=%s failed; the sweep retries it", self.thread_id)

    @classmethod
    def keep_queued_claims_alive(cls) -> int:
        """Heartbeat the slots the drain claimed for runs whose task still waits for a worker, which nothing else beats
        until the task starts; returns how many it beat. SYNC ONLY."""
        waiting = list(
            Run.objects.filter(status=RunStatus.READY, task_result__status=TaskResultStatus.READY).values_list(
                "pk", "session_id"
            )
        )
        return Session.objects.filter(
            thread_id__in={session_id for _, session_id in waiting}, active_run_id__in=[str(pk) for pk, _ in waiting]
        ).update(last_active_at=timezone.now())

    @classmethod
    def sessions_to_drain(cls, limit: int = DRAIN_SWEEP_LIMIT) -> list[str]:
        """Sessions with a pending waking event and a free or stale slot, for the sweep: an archived one only for a leg
        follow-up or merge request job its drain stops. SYNC ONLY."""
        free = Q(session__active_run_id__isnull=True) | Q(session__last_active_at__lt=stale_cutoff())
        takes_it = Q(session__archived_at__isnull=True) | Q(kind__in=OWN_RUN_KINDS)
        return list(
            InboxEvent.objects
            .filter(free, takes_it, state=EventState.PENDING, wake=True)
            .order_by()
            .values_list("session_id", flat=True)
            .distinct()[:limit]
        )

    @transaction.atomic
    def _take(self) -> tuple[Run, list[_Charge]] | None:
        session = Session.objects.select_for_update(of=("self",)).select_related("user").get(pk=self.thread_id)
        return _Take(session).take()

    def _undo_take(self, run: Run, charges: list[_Charge]) -> None:
        """Free the slot claimed for ``run``, put its events back and refund the CI attempts its take spent, after its
        enqueue failed. The slot and events are usually back already: the failed run's ``run_ended`` did both
        (:meth:`sessions.inbox.complete.RunEnd.on_run_ended`), unless its finish did not land. A leg follow-up or a
        merge request job stays with its run, whose end settles it.

        A refund leaves the merge request's examined commit blank, as the one before is not kept. That commit is older
        than the failure's, so it is no longer the head DAIV pushed, the only commit whose failure wakes the session. A
        failure another drain took after ``run_ended`` put it back keeps its charge: that run did not charge the commit
        again. The session row lock orders the check against such a take.
        """
        with transaction.atomic():
            Session.objects.select_for_update().filter(pk=run.session_id).exists()
            SessionLock.release_for_run(run.session_id, run.pk)
            InboxEvent.objects.filter(run=run, state=EventState.TAKEN).exclude(kind__in=OWN_RUN_KINDS).update(
                state=EventState.PENDING, run=None, updated_at=timezone.now()
            )
            retaken = set(
                InboxEvent.objects.filter(
                    pk__in=[charge.event_id for charge in charges], state=EventState.TAKEN
                ).values_list("pk", flat=True)
            )
            for charge in charges:
                if charge.event_id in retaken:
                    continue
                rows = MergeRequestOwnership.objects.filter(
                    repo_id=charge.repo_id, merge_request_iid=charge.merge_request_iid
                )
                rows.filter(attempts__gt=0).update(attempts=F("attempts") - 1)
                rows.filter(last_examined_sha=charge.sha).update(last_examined_sha="")
            session_changed(run.session_id)


class _Charge(NamedTuple):
    """A CI attempt the take spent: the merge request, the commit it recorded as examined, the failure that woke."""

    repo_id: str
    merge_request_iid: int
    sha: str
    event_id: uuid.UUID


class _Take:
    """One take of ``session``'s pending events, run in :meth:`Drain._take`'s transaction with the session row
    locked."""

    def __init__(self, session: Session):
        self.session = session

    def take(self) -> tuple[Run, list[_Charge]] | None:
        session = self.session
        next_runs = NextRuns(session)
        if session.archived_at is not None:
            for held in InboxEvent.objects.filter(session=session, state=EventState.PENDING, kind__in=OWN_RUN_KINDS):
                self._stop_or_settle(held)
            return None
        if SessionLock.is_held(session) or next_runs.job_run_active():
            return None
        pending = list(
            InboxEvent.objects
            .filter(session=session, state=EventState.PENDING)
            .select_related("author")
            .order_by("created_at", "id")
        )
        pending_events = PendingEvents(session, pending)
        queued_runs = list(next_runs.queued())
        pending_events.follow_session_branch(queued_runs)
        queued: dict[tuple[str, Requester], Run] = {}
        for next_run in queued_runs:
            queued.setdefault((next_run.ref, Requester.of_run(next_run)), next_run)
        survivors = pending_events.supersede()
        self._apply_policy(survivors)
        branches = {event.pk: next_runs.branch_of(event) for event in survivors}
        waking = [event for event in survivors if event.wake]
        wanted = {(branches[event.pk], Requester.of_event(event, session)) for event in waking}
        next_runs.drop([run for key, run in queued.items() if key not in wanted])
        if not waking:
            return None

        branch, requester = branches[waking[0].pk], Requester.of_event(waking[0], session)
        requested = [
            event
            for event in waking
            if branches[event.pk] in ("", branch) and Requester.of_event(event, session) == requester
        ]
        splits = RequestSplits(requested)
        asked = splits.splits[0]
        own_run = None
        if asked[0].kind in OWN_RUN_KINDS and (own_run := self._stop_or_settle(asked[0])) is None:
            transaction.on_commit(Drain(session.thread_id).start_logged, robust=True)
            return None
        if RequestSplits.takes_context(asked[0]):
            taken = [
                event
                for event in survivors
                if branches[event.pk] in ("", branch) and (not event.wake or event in asked)
            ]
        else:
            taken = asked
        waiting_run = queued.get((branch, requester))
        if own_run is not None:
            next_run = own_run
        elif waiting_run is not None and splits.holding(waiting_run.pk) == 0:
            next_run = waiting_run
        else:
            next_run = None
        user = requester.user(session, requested)
        refused: tuple[list[InboxEvent], Run | None] | None = None
        if own_run is not None and not own_run.acts_as_user:
            if not self._proves_access(user, asked[0]):
                refused = (asked, None)
            user = None
        elif user is not None:
            acting = self._acting_as_requester(requested)
            if any(event.kind != EventKind.MENTION for event in acting) and not (
                self._may_run(user, session.repo_ids()) and self._may_read(user, session.submodule_repo_ids())
            ):
                refused = (acting, waiting_run)
        if refused is not None:
            self._fail(*refused)
            next_runs.drop_emptied(queued_runs)
            transaction.on_commit(Drain(session.thread_id).start_logged, robust=True)
            return None

        env_id = self._sandbox_env_id(user, taken)
        try:
            with transaction.atomic():
                run = self._promote_or_create(
                    branch,
                    taken,
                    next_run,
                    requester=None if own_run is not None else requester,
                    sandbox_environment_id=env_id,
                )
        except IntegrityError:
            # A job run took the session's one active place after ``NextRuns.job_run_active`` looked
            # (``run_one_active_per_session``).
            logger.info(
                "inbox: thread_id=%s has an active job run; its events wait for that run to end", session.thread_id
            )
            next_runs.drop_emptied(queued_runs)
            return None
        if not SessionLock.claim_for_run(session.thread_id, run.pk):
            transaction.set_rollback(True)
            return None

        InboxEvent.objects.filter(pk__in=[event.pk for event in taken]).update(
            state=EventState.TAKEN, run=run, claim_token="", updated_at=timezone.now()
        )
        self._link_superseded(taken, run, branch)
        next_runs.drop_emptied(queued_run for queued_run in queued_runs if queued_run.pk != run.pk)
        if waiting_run is not None and waiting_run.pk != run.pk:
            next_runs.describe(waiting_run)
        self._merge_references(taken)
        self._apply_tools_selection(taken)
        return run, self._charge_ci_attempts(taken)

    def _stop_or_settle(self, own: InboxEvent, *, failure: EventFailure | None = None) -> Run | None:
        """The own run of the leg follow-up or merge request job ``own``, locked, while it may start; else ``None``,
        once ``own`` is settled because its run is gone or no longer QUEUED, or stopped with ``failure``, else because
        its session was archived or its merge request ended. SYNC ONLY."""
        own_runs = OWN_RUNS[own.kind]
        # Locked so a cancel can't land between this read and the stop or the promotion to READY.
        run = Run.objects.select_for_update().filter(pk=own.run_id).first()
        if run is None or run.status != RunStatus.QUEUED:
            own_runs.settle_unstartable(own, run)
            return None
        if (stop := failure or own_runs.stop_reason(self.session, own)) is not None:
            own_runs.stop(own, stop, run)
            return None
        return run

    def _link_superseded(self, taken: list[InboxEvent], run: Run, branch: str) -> None:
        """Link to ``run`` the superseded events of the subjects it took, on its ``branch``, which it shows replaced."""
        subjects = {event.subject_key for event in taken if event.subject}
        if not subjects:
            return
        InboxEvent.objects.filter(
            reduce(or_, (Q(repo_id=repo_id, subject=subject) for repo_id, subject in subjects)),
            NextRuns(self.session).events_on(branch),
            session=self.session,
            state=EventState.SUPERSEDED,
            run__isnull=True,
        ).update(run=run)

    def _merge_references(self, taken: list[InboxEvent]) -> None:
        """Add the references the follow-up jobs and leg follow-ups the run took declared to the session, which the run
        reads them from; a merge request job's apply to its run only."""
        session = self.session
        declared = [
            ref
            for event in taken
            if event.kind in (EventKind.FOLLOW_UP_JOB, EventKind.LEG_FOLLOW_UP)
            for ref in event.payload.get("references", [])
        ]
        if declared and (merged := merge_stored_refs(session.external_refs, declared)) != session.external_refs:
            Session.objects.filter(pk=session.pk).update(external_refs=merged)

    def _apply_tools_selection(self, taken: list[InboxEvent]) -> None:
        """Give the session the Tools selection the newest chat message the run took was sent with, which
        ``run_job_task`` reads when the run starts."""
        selections = [event.payload["mcp_overrides"] for event in taken if "mcp_overrides" in event.payload]
        if selections and selections[-1] != self.session.mcp_overrides:
            Session.objects.filter(pk=self.session.pk).update(mcp_overrides=selections[-1])

    def _charge_ci_attempts(self, taken: list[InboxEvent]) -> list[_Charge]:
        """Spend an attempt of each merge request whose CI failure woke the run taking ``taken``, and record the commit
        it examines; return the charges. A run that takes the failure again after a failed one examines the same
        commit, and is not charged again."""
        charges = []
        for event in taken:
            if event.kind != EventKind.CI_FAILED or not event.wake or event.merge_request_iid is None:
                continue
            sha, repo_id = event.payload["sha"], event.effective_repo_id(self.session.repo_id)
            rows = MergeRequestOwnership.objects.filter(repo_id=repo_id, merge_request_iid=event.merge_request_iid)
            if rows.exclude(last_examined_sha=sha).update(last_examined_sha=sha, attempts=F("attempts") + 1):
                charges.append(_Charge(repo_id, event.merge_request_iid, sha, event.pk))
        return charges

    def _apply_policy(self, survivors: list[InboxEvent]) -> None:
        """Turn the waking events the policy blocks into remembered ones, recording why. A CI failure whose merge
        request was closed, or whose watch was turned off, since it was posted no longer wakes either."""
        reasons = WakePolicy(self.session).quiet_reasons_now(survivors)
        for event in survivors:
            if (reason := reasons.get(event.pk)) is not None:
                event.wake, event.reason = False, reason
                event.save(update_fields=["wake", "reason", "updated_at"])
        if reasons:
            session_changed(self.session.thread_id)
        if QuietReason.RUN_LIMIT in reasons.values():
            transaction.on_commit(WakePolicy(self.session).report_run_limit, robust=True)

    def _proves_access(self, user: User | None, job: InboxEvent) -> bool:
        """Whether a job only attributed to ``user`` (a CI identity's) may run in the session. The platform proved the
        job's own repository, so ``user`` must be able to run on the session's other repositories, which its run checks
        out too, and read the submodules DAIV fetched in it; with no linked user, there must be none of either."""
        others = [
            repo_id for repo_id in self.session.repo_ids() if repo_id != job.effective_repo_id(self.session.repo_id)
        ]
        submodules = self.session.submodule_repo_ids()
        if user is None:
            return not others and not submodules
        return (not others or self._may_run(user, others)) and self._may_read(user, submodules)

    @staticmethod
    def _acting_as_requester(events: list[InboxEvent]) -> list[InboxEvent]:
        """``events`` but those whose own run is only attributed to its requester (``Run.acts_as_user`` off), which
        neither need the requester's access nor fail without it."""
        attributed = set(
            Run.objects.filter(
                pk__in=[event.run_id for event in events if event.kind in OWN_RUN_KINDS], acts_as_user=False
            ).values_list("pk", flat=True)
        )
        return [event for event in events if event.run_id not in attributed]

    @staticmethod
    def _may_run(user: User, repo_ids: list[str]) -> bool:
        try:
            assert_repo_access(user, repo_ids)
        except RepoAccessError:
            return False
        return True

    @staticmethod
    def _may_read(user: User, repo_ids: list[str]) -> bool:
        return not repo_ids or viewable_repo_ids(user, repo_ids) >= set(repo_ids)

    def _fail(self, asked: list[InboxEvent], next_run: Run | None) -> None:
        """Fail the events of a requester who may no longer run on the repository and finish their QUEUED next run
        FAILED with the ``access_denied`` label, so the id a job's caller polls still resolves. Each leg follow-up and
        merge request job among them is stopped with that failure (:meth:`_stop_or_settle`), so a job says so on its
        merge request and a leg follow-up's batch finishes; those events stay linked to their runs, which is how a batch
        tells the run apart.

        The finishes say ``skip_dispatch``: the caller drains once this commits, and the events, already failed rather
        than taken, are left as they are by the run's end (:meth:`sessions.inbox.complete.RunEnd.settle_events`)."""
        logger.warning(
            "inbox: a requester on thread_id=%s may no longer run on %s; %d event(s) failed",
            self.session.thread_id,
            self.session.repo_id,
            len(asked),
        )
        now = timezone.now()
        InboxEvent.objects.filter(pk__in=[event.pk for event in asked]).exclude(kind__in=OWN_RUN_KINDS).update(
            state=EventState.FAILED, failure=EventFailure.ACCESS_DENIED, run=None, updated_at=now
        )
        session_changed(self.session.thread_id)
        if next_run is not None:
            InboxEvent.objects.filter(run=next_run).update(run=None, updated_at=now)
            Run.objects.finish(
                next_run,
                RunStatus.FAILED,
                skip_dispatch=True,
                never_started=True,
                error_message=str(EventFailure.ACCESS_DENIED.label),
                finished_at=now,
                started_at=now,
            )
        for event in asked:
            if event.kind in OWN_RUN_KINDS:
                self._stop_or_settle(event, failure=EventFailure.ACCESS_DENIED)

    def _sandbox_env_id(self, user: User | None, taken: list[InboxEvent]) -> uuid.UUID | None:
        """The environment the newest job among ``taken`` was submitted with, while ``user`` still sees it.
        Else the session's, unless it is a USER environment ``user`` does not own: a run for another person, or for no
        one, never gets the owner's secrets. Then the one the repository resolves to for ``user``."""
        visible = SandboxEnvironment.objects.visible_to(user)  # ty: ignore[missing-attribute]
        asked = [
            event.payload["sandbox_environment_id"] for event in taken if event.payload.get("sandbox_environment_id")
        ]
        if asked and visible.filter(pk=asked[-1]).exists():
            return uuid.UUID(asked[-1])
        env_id = self.session.sandbox_environment_id
        if env_id is None or visible.filter(pk=env_id).exists():
            return env_id
        env = async_to_sync(resolve_env_for_run)(user=user, repo_id=self.session.repo_id)
        return None if env is None else env.pk

    def _promote_or_create(
        self,
        branch: str,
        taken: list[InboxEvent],
        run: Run | None,
        *,
        requester: Requester | None,
        sandbox_environment_id: uuid.UUID | None,
    ) -> Run:
        """Make ``run``, else a new run, READY to run ``taken`` on ``branch`` for ``requester``. ``None`` keeps the
        person ``run`` names: an own run's, whose post named its caller's platform account, which the requester leaves
        out."""
        session = self.session
        waking = sorted(
            (event for event in taken if event.wake), key=lambda event: (PRECEDENCE[event.kind], event.created_at)
        )
        top = waking[0]
        person = next((event for event in waking if event.kind in PERSON_KINDS), None)
        agent_model, agent_thinking_level = self._agent_override(person)
        chats = [event for event in taken if event.kind == EventKind.CHAT_MESSAGE]
        fields = {
            "status": RunStatus.READY,
            "ref": branch,
            "trigger_type": EventContent.of(top).run_trigger_type(),
            "agent_model": agent_model,
            "agent_thinking_level": agent_thinking_level,
            "message_id": chats[-1].payload["agui_message_id"] if chats else "",
            "merge_request_iid": next(
                (event.merge_request_iid for event in waking if event.merge_request_iid and not event.repo_id), None
            ),
            "sandbox_environment_id": sandbox_environment_id,
            "prompt": RunInput(taken).request(),
            "muted": (person or top).payload.get("muted"),
        }
        if requester is not None:
            fields |= requester.run_fields(person.author_username if person is not None else "")
        if run is None:
            return Run.objects.create(session=session, repo_id=session.repo_id, **fields)
        for name, value in fields.items():
            setattr(run, name, value)
        run.save(update_fields=list(fields))
        return run

    def _agent_override(self, person: InboxEvent | None) -> tuple[str, str]:
        if person is not None and person.payload.get("agent_model"):
            return person.payload["agent_model"], person.payload.get("agent_thinking_level", "")
        return pinned_agent_override(self.session)
