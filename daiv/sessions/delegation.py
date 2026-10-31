"""Delegated jobs: a coordinator run fans tailored sub-jobs (legs) out with ``delegate_jobs``, to other repositories or
to new sessions on its own, ends its turn, and is resumed once every leg has finished.

:class:`SessionJobDelegator` is the agent's ``automation.agent.delegation.JobDelegator`` for executor runs, and
:func:`adelegator_for` hands one only to a session that may delegate. It submits the legs as one batch of
``SessionOrigin.DELEGATED_JOB`` runs as the user who triggered the delegating run, muted when that run is, each in a
new session whose ``parent`` is the coordinator's. Delegation is one level deep: a session with a parent gets no
delegator, so a leg's first run is its whole answer and the batch is done when every leg's run is. Failing legs cannot
loop a coordinator forever: the inbox stops waking a session after ``sessions.inbox.policy.MAX_SYSTEM_RUNS_IN_A_ROW``
runs in a row that nobody asked for.

A leg always publishes to a new branch and merge request of its own (``run_job_task``'s ``fork_branch``), so any
number of legs may share a base, the coordinator's own included. A leg that names the coordinator's own branch
``waits_for`` the delegating run: it stays QUEUED until that run has finished, and so has published what it changed
this turn, and the queue starts it then (``sessions.queue.release_runs_waiting_for``), from the branch the
coordinator's session works on by then, which differs when the coordinator published to a new branch, as a run on
the default branch always does.

:func:`resume_coordinator` runs on every leg's ``run_finished``. Once the whole batch is terminal it posts one waking
``delegation_finished`` event to the coordinator's inbox (source key ``batch:<id>``, so several legs finishing at once
post it once), whose summary covers every leg without quoting DAIV's own error text. The inbox starts the
coordinator's ``SessionOrigin.DELEGATION_RESUME`` run once the session is free, as its owner and after checking the
owner may still run on its repository. A batch none of whose legs reached the task queue or waited for the
coordinator resumes nothing: the tool already told the agent nothing started, and neither does one whose every leg
was cancelled. ``continuation_of_batch_id`` is no longer written; it stays readable on older continuations.

``SessionJobDelegator.astatus`` reports the session's batches, its latest ``MAX_STATUS_BATCHES`` without a batch id,
and ``acancel`` asks a batch's unfinished legs to stop through ``RunManager.cancel``: a leg that is not running yet
finishes FAILED at once, and a running one is ``stopping`` until its ``run_job_task`` stops at its next check and it
finishes as that task does. Neither quotes DAIV's own error text, as the continuation's prompt does not.

:func:`resume_stalled_batches` is the periodic backstop: it posts the event for a finished batch the signal path left
without one, and releases the legs and older continuations left QUEUED on a session with no active run once they are
not waiting. Retrying a resume that failed is the inbox's own job.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone

from asgiref.sync import sync_to_async
from sandbox_envs.selection import aresolve_repo_envs

from automation.agent.delegation import (
    MAX_DELEGATED_TARGETS,
    BatchStatus,
    CancelResult,
    DelegationError,
    DelegationResult,
    FailedLeg,
    LegStatus,
    StartedLeg,
)
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE
from core.utils import build_absolute_url
from sessions.admission import AdmissionError, RepoAccessError, aassert_repo_access, check_agent_override
from sessions.artifacts import aresolve_active_run
from sessions.inbox import kinds
from sessions.inbox.post import post_event
from sessions.models import EventKind, InboxEvent, Run, RunStatus, Session, SessionOrigin
from sessions.queue import LINK_FAILED_PREFIX, release_orphan_queued_runs
from sessions.services import RepoTarget, asubmit_batch_runs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from automation.agent.delegation import Checkout, DelegatedTarget
    from sessions.services import BatchSubmitFailure

logger = logging.getLogger("daiv.sessions")

LEG_REPLY_MAX_CHARS = 1000
MAX_STATUS_BATCHES = 5
STALLED_BATCH_WINDOW = timedelta(days=1)

_OUTCOMES = {
    RunStatus.QUEUED: "queued",
    RunStatus.READY: "queued",
    RunStatus.RUNNING: "running",
    RunStatus.SUCCESSFUL: "succeeded",
    RunStatus.FAILED: "failed",
    RunStatus.WAITING_INPUT: "stopped to ask a question",
}
_UNSTARTED_LEG = "DAIV could not start this leg; the error is on DAIV's side, not in the request."
_UNTRACKED_LEG = (
    "This leg started, but DAIV lost track of it: it may still open a merge request, and its outcome will not "
    "reach you."
)


async def adelegator_for(thread_id: str) -> SessionJobDelegator | None:
    """The delegator for a run on ``thread_id``; ``None`` for a delegated session, which cannot delegate further."""
    if await Session.objects.filter(pk=thread_id, parent__isnull=False).aexists():
        return None
    return SessionJobDelegator(thread_id)


class SessionJobDelegator:
    """Submits ``delegate_jobs`` legs for the run on ``thread_id``, as the user who triggered it."""

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id

    async def adelegate(self, *, checkout: Checkout, goal: str, targets: Sequence[DelegatedTarget]) -> DelegationResult:
        run = await _aactive_run(self._thread_id)
        if run is None:
            raise DelegationError("This run has no session to resume, so it cannot delegate.")
        if run.user is None:
            raise DelegationError(
                "Sub-jobs run as the DAIV user who triggered this run, and it has none, so it cannot delegate."
            )
        if not goal:
            raise DelegationError("`goal` must name the overall objective.")
        if not targets:
            raise DelegationError("Give at least one target.")
        if len(targets) > MAX_DELEGATED_TARGETS:
            raise DelegationError(f"At most {MAX_DELEGATED_TARGETS} targets per call; split the work across calls.")
        if any(not target.repo_id or not target.prompt for target in targets):
            raise DelegationError("Every target needs a `repo_id` and a prompt of its own.")

        user = run.user
        denied: set[str] = set()
        try:
            await aassert_repo_access(user, [target.repo_id for target in targets])
        except RepoAccessError as err:
            denied = set(err.repo_ids)
        failed = [FailedLeg(t.repo_id, t.ref, REPO_ACCESS_DENIED_MESSAGE) for t in targets if t.repo_id in denied]
        allowed = [t for t in targets if t.repo_id not in denied]
        if not allowed:
            return DelegationResult(batch_id=None, failed=failed)

        try:
            check_agent_override(None, None)
            repos = await aresolve_repo_envs(
                user=user,
                repos=[
                    RepoTarget(
                        repo_id=t.repo_id,
                        ref=t.ref,
                        prompt=t.prompt,
                        references=t.references,
                        waits_for_run_id=run.pk if _on_own_branch(checkout, t) else None,
                    )
                    for t in allowed
                ],
                explicit_env_id=None,
            )
            result = await asubmit_batch_runs(
                user=user,
                prompt=goal,
                repos=repos,
                trigger_type=SessionOrigin.DELEGATED_JOB,
                muted=run.effective_muted,
                parent=run.session,
            )
        except AdmissionError as err:
            raise DelegationError(str(err)) from err

        started = await sync_to_async(
            lambda: [
                StartedLeg(
                    leg.repo_id,
                    leg.ref,
                    str(leg.session_id),
                    _session_url(leg.session_id),
                    starts_after_this_turn=leg.waits_for_id is not None,
                )
                for leg in result.runs
            ]
        )()
        return DelegationResult(
            batch_id=str(result.batch_id),
            started=started,
            failed=[*failed, *(_failed_leg(failure) for failure in result.failed)],
        )

    async def astatus(self, *, batch_id: str | None) -> list[BatchStatus]:
        return await sync_to_async(self._status)(batch_id)

    async def acancel(self, *, batch_id: str, thread_ids: Sequence[str]) -> CancelResult:
        return await sync_to_async(self._cancel)(batch_id, set(thread_ids))

    def _status(self, batch_id: str | None) -> list[BatchStatus]:
        if batch_id:
            return [_batch_status(self._batch_legs(batch_id))]
        latest = list(
            self
            ._legs()
            .values("batch_id")
            .annotate(latest=Max("created_at"))
            .order_by("-latest")
            .values_list("batch_id", flat=True)[:MAX_STATUS_BATCHES]
        )
        by_batch: dict[uuid.UUID, list[Run]] = defaultdict(list)
        for leg in self._legs().filter(batch_id__in=latest).order_by("created_at", "id"):
            by_batch[leg.batch_id].append(leg)
        return [_batch_status(by_batch[batch]) for batch in latest]

    def _cancel(self, batch_id: str, thread_ids: set[str]) -> CancelResult:
        legs = self._batch_legs(batch_id)
        if unknown := thread_ids - {leg.session_id for leg in legs}:
            raise DelegationError(f"Not legs of batch {batch_id}: {', '.join(sorted(unknown))}.")
        cancelled = [
            leg.session_id
            for leg in legs
            if (not thread_ids or leg.session_id in thread_ids)
            and leg.status not in RunStatus.terminal()
            and Run.objects.cancel(leg)
        ]
        return CancelResult(batch=_batch_status(legs), cancelled=cancelled, resumes=_resumes(legs))

    def _legs(self) -> QuerySet[Run]:
        return Run.objects.filter(
            trigger_type=SessionOrigin.DELEGATED_JOB, session__parent_id=self._thread_id
        ).select_related("session")

    def _batch_legs(self, batch_id: str) -> list[Run]:
        try:
            batch = uuid.UUID(batch_id)
        except ValueError:
            raise DelegationError(f"`{batch_id}` is not a batch id: give one delegate_jobs returned.") from None
        if legs := list(self._legs().filter(batch_id=batch).order_by("created_at", "id")):
            return legs
        raise DelegationError(f"This session delegated no batch {batch_id}.")


async def _aactive_run(thread_id: str) -> Run | None:
    if (run := await aresolve_active_run(thread_id)) is None:
        return None
    return await Run.objects.select_related("session__scheduled_job", "user").aget(pk=run.pk)


def _on_own_branch(own: Checkout, target: DelegatedTarget) -> bool:
    """Whether ``target`` names the delegating run's own branch, to build on what that run publishes when it ends."""
    return target.repo_id.casefold() == own.repo_id.casefold() and bool(target.ref) and target.ref == own.ref


def _failed_leg(failure: BatchSubmitFailure) -> FailedLeg:
    """``failure`` for the agent, which may quote it publicly: its error stays in the logs and on the run."""
    if failure.task_enqueued:
        return FailedLeg(failure.repo_id, failure.ref, _UNTRACKED_LEG, still_runs=True)
    return FailedLeg(failure.repo_id, failure.ref, _UNSTARTED_LEG)


def _session_url(thread_id: str) -> str:
    return build_absolute_url(reverse("session_detail", kwargs={"thread_id": thread_id}))


def _batch_status(legs: Sequence[Run]) -> BatchStatus:
    return BatchStatus(
        batch_id=str(legs[0].batch_id),
        finished=all(leg.status in RunStatus.terminal() for leg in legs),
        legs=[_leg_status(leg) for leg in legs],
    )


def _leg_status(leg: Run) -> LegStatus:
    return LegStatus(
        repo_id=leg.repo_id,
        ref=leg.ref,
        thread_id=leg.session_id,
        session_url=_session_url(leg.session_id),
        status=_leg_outcome(leg),
        branch=leg.session.ref if leg.session.ref != leg.ref else "",
        merge_request_url=leg.merge_request_web_url,
        reply=leg.result_summary.strip()[:LEG_REPLY_MAX_CHARS],
    )


def _resumes(legs: Sequence[Run]) -> bool:
    """Whether ``legs``' batch resumes its coordinator once it has finished: some leg reached the task queue or waited
    for the coordinator, and not every leg was cancelled or is stopping."""
    return any(leg.task_result_id or leg.waits_for_id for leg in legs) and not all(
        leg.is_cancelled or _stopping(leg) for leg in legs
    )


def _stopping(leg: Run) -> bool:
    """Whether ``leg`` was asked to stop and has not finished yet."""
    return leg.cancel_requested_at is not None and leg.status not in RunStatus.terminal()


def resume_coordinator(run: Run) -> None:
    """Queue the coordinator's continuation once ``run``'s delegated batch has finished; the module docstring says
    when nothing is queued."""
    if run.trigger_type != SessionOrigin.DELEGATED_JOB or run.batch_id is None:
        return
    _resume_batch(run.batch_id)


def _resume_batch(batch_id: uuid.UUID) -> bool:
    """Post ``batch_id``'s ``delegation_finished`` event if the batch has finished and resumes its coordinator;
    returns whether this call posted it."""
    batch = Run.objects.by_batch(batch_id)
    if batch.exclude(status__in=RunStatus.terminal()).exists():
        return False
    legs = list(batch.select_related("user", "session__parent").order_by("created_at", "id"))
    if not _resumes(legs):
        return False
    coordinator = legs[0].session.parent
    if coordinator is None:
        logger.info("resume_coordinator: batch %s has no coordinator session left to resume", batch_id)
        return False
    result = post_event(
        coordinator.pk,
        kind=EventKind.DELEGATION_FINISHED,
        source_key=kinds.batch_key(batch_id),
        payload={"batch_id": str(batch_id), "summary": render_batch_summary(batch_id, legs)},
        wake=True,
        author=legs[0].user,
    )
    return result.created


def resume_stalled_batches() -> int:
    """Resume the coordinators the ``run_finished`` path left waiting; returns how many events it posted."""
    finished_recently = Run.objects.filter(
        trigger_type=SessionOrigin.DELEGATED_JOB, finished_at__gte=timezone.now() - STALLED_BATCH_WINDOW
    ).values("batch_id")
    batch_ids = list(
        Run.objects
        .filter(trigger_type=SessionOrigin.DELEGATED_JOB, batch_id__in=finished_recently)
        .exclude(
            batch_id__in=Run.objects.filter(continuation_of_batch_id__isnull=False).values("continuation_of_batch_id")
        )
        .values("batch_id")
        .annotate(unfinished=Count("id", filter=~Q(status__in=RunStatus.terminal())))
        .filter(unfinished=0)
        .values_list("batch_id", flat=True)
    )
    posted = set(
        InboxEvent.objects.filter(source_key__in=[kinds.batch_key(batch_id) for batch_id in batch_ids]).values_list(
            "source_key", flat=True
        )
    )
    resumed = 0
    for batch_id in batch_ids:
        if kinds.batch_key(batch_id) in posted:
            continue
        try:
            if _resume_batch(batch_id):
                resumed += 1
        except Exception:
            logger.exception("resume_stalled_batches: failed to resume batch %s", batch_id)

    waiting = Run.objects.filter(
        Q(waits_for__isnull=False) | Q(continuation_of_batch_id__isnull=False), status=RunStatus.QUEUED
    )
    if waiting.exists():
        try:
            release_orphan_queued_runs(session_ids=waiting.values("session_id"))
        except Exception:
            logger.exception("resume_stalled_batches: failed to release the queued legs and continuations")
    return resumed


def render_batch_summary(batch_id: uuid.UUID, legs: Sequence[Run]) -> str:
    """The continuation's prompt: each leg's outcome, session, branch, merge request and reply."""
    succeeded = sum(leg.status == RunStatus.SUCCESSFUL for leg in legs)
    lines = [f"The sub-jobs you delegated (batch {batch_id}) have finished: {succeeded} of {len(legs)} succeeded.", ""]
    for leg in legs:
        info = _leg_status(leg)
        where = f"{info.repo_id} on {info.ref}" if info.ref else info.repo_id
        lines.append(f"## {where}: {info.status}")
        lines.append(f"- Session: {info.session_url}")
        if info.branch:
            lines.append(f"- Branch: {info.branch}")
        if info.merge_request_url:
            lines.append(f"- Merge request: {info.merge_request_url}")
        if info.reply:
            # Quoted so a leg's reply can't pass for a sibling's section or for these instructions.
            lines.append("- Reply:")
            lines.extend(f"  > {line}" for line in info.reply.splitlines())
        elif leg.status == RunStatus.FAILED and not leg.is_cancelled:
            lines.append("- Reply: none; open its session to see what happened.")
        lines.append("")
    lines.append(
        "Check the outcome and report it where the request came from. A leg's work is on its branch: merge that "
        "branch into yours if it belongs in this change, or leave its merge request for review. Delegate another "
        "batch if follow-up work depends on these results."
    )
    lines.append(
        "This message comes from DAIV, not from the user: if you asked the user a question they have not answered, "
        "this is not their answer, so ask it again if you still need it."
    )
    return "\n".join(lines)


def _leg_outcome(leg: Run) -> str:
    if _stopping(leg):
        return "stopping"
    if leg.is_cancelled:
        return "cancelled"
    if leg.status == RunStatus.FAILED and leg.error_message.startswith(LINK_FAILED_PREFIX):
        return "started, but DAIV lost track of it"
    return _OUTCOMES.get(leg.status, leg.status)
