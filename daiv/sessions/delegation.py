"""Delegated jobs: a coordinator run fans tailored sub-jobs (legs) out with ``delegate_jobs``, to other repositories or
to new sessions on its own, ends its turn, and is resumed once every leg has finished.

:class:`SessionJobDelegator` is the agent's ``automation.agent.delegation.JobDelegator`` for executor runs, and
:func:`adelegator_for` hands one only to a session that may delegate. It submits the legs as one batch of
``SessionOrigin.DELEGATED_JOB`` runs as the user who triggered the delegating run, muted when that run is, each in a
new session whose ``parent`` is the coordinator's. Delegation is one level deep: a session with a parent gets no
delegator, so a leg's first run is its whole answer and the batch is done when every leg's run is. A coordinator
resumed ``MAX_RESUMES_IN_A_ROW`` times in a row by its own delegations may not delegate again until another run
on its session, so failing legs cannot loop it forever.

A leg always publishes to a new branch and merge request of its own (``run_job_task``'s ``fork_branch``), so any
number of legs may share a base, the coordinator's own included. A leg that names the coordinator's own branch
``waits_for`` the delegating run: it stays QUEUED until that run has finished, and so has published what it changed
this turn, and the queue starts it then (``sessions.queue.release_runs_waiting_for``), from the branch the
coordinator's session works on by then, which differs when the coordinator published to a new branch, as a run on
the default branch always does.

:func:`resume_coordinator` runs on every leg's ``run_finished``. Once the whole batch is terminal it queues one
``SessionOrigin.DELEGATION_RESUME`` run on the coordinator's session (``continuation_of_batch_id``), as the legs'
user and with their mute, whose prompt summarises every leg without quoting DAIV's own error text. The continuation
``waits_for`` whichever run is active on the coordinator's session then, whatever its trigger, and is released like
any QUEUED run once it is not waiting. When several legs finish at once, ``run_one_continuation_per_batch`` lets one
insert win. A user who has lost WRITE access to the coordinator's repository meanwhile gets the continuation FAILED
instead. A batch none of whose legs reached the task queue or waited for the coordinator resumes nothing: the tool
already told the agent nothing started, and neither does one whose every leg was cancelled.

``SessionJobDelegator.astatus`` reports the session's batches, its latest ``MAX_STATUS_BATCHES`` without a batch id,
and ``acancel`` asks a batch's unfinished legs to stop through ``RunManager.cancel``: a leg that is not running yet
finishes FAILED at once, and a running one is ``stopping`` until its ``run_job_task`` stops at its next check and it
finishes as that task does. Neither quotes DAIV's own error text, as the continuation's prompt does not.

:func:`resume_stalled_batches` is the periodic backstop: it resumes a finished batch the signal path left without a
continuation, retries one whose continuation never ran (it never reached the task queue, or gave up waiting for the
session's slot; up to ``MAX_CONTINUATION_ATTEMPTS`` continuations per batch), and releases the legs and
continuations left QUEUED on a session with no active run once they are not waiting.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
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
from sessions.admission import (
    AdmissionError,
    RepoAccessError,
    aassert_repo_access,
    assert_repo_access,
    check_agent_override,
)
from sessions.artifacts import aresolve_active_run
from sessions.executor.lock import SessionLockTimeoutError
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.queue import ENQUEUE_FAILED_PREFIX, LINK_FAILED_PREFIX, dispatch_next_queued, release_orphan_queued_runs
from sessions.services import RepoTarget, asubmit_batch_runs, create_run, pinned_agent_override

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from accounts.models import User
    from automation.agent.delegation import Checkout, DelegatedTarget
    from sessions.services import BatchSubmitFailure

logger = logging.getLogger("daiv.sessions")

LEG_REPLY_MAX_CHARS = 1000
MAX_CONTINUATION_ATTEMPTS = 3
MAX_RESUMES_IN_A_ROW = 3
MAX_STATUS_BATCHES = 5
STALLED_BATCH_WINDOW = timedelta(days=1)
ACCESS_DENIED_PREFIX = "access_denied"

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
# A run_job_task that gave up waiting for the session's slot stores its exception's class path as the run's error.
_LOCK_TIMED_OUT = f"{SessionLockTimeoutError.__module__}.{SessionLockTimeoutError.__qualname__}"
_NEVER_RAN = Q(status=RunStatus.FAILED) & (
    Q(error_message__startswith=ENQUEUE_FAILED_PREFIX) | Q(error_message__startswith=_LOCK_TIMED_OUT)
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
        if await _aresumes_in_a_row(run) >= MAX_RESUMES_IN_A_ROW:
            raise DelegationError(
                f"Your own delegations have resumed you {MAX_RESUMES_IN_A_ROW} times in a row, so you cannot delegate "
                "again. Finish what you can in this turn and report what is left where the request came from."
            )

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


async def _aresumes_in_a_row(run: Run) -> int:
    """How many batches have resumed ``run``'s session in a row, up to ``run``, with no other run in between."""
    if run.trigger_type != SessionOrigin.DELEGATION_RESUME:
        return 0
    session_runs = Run.objects.filter(session_id=run.session_id, created_at__lte=run.created_at)
    last_other = await (
        session_runs
        .exclude(trigger_type=SessionOrigin.DELEGATION_RESUME)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .afirst()
    )
    resumes = session_runs.filter(trigger_type=SessionOrigin.DELEGATION_RESUME)
    if last_other is not None:
        resumes = resumes.filter(created_at__gt=last_other)
    return await resumes.order_by().values("continuation_of_batch_id").distinct().acount()


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
    """Queue ``batch_id``'s continuation if the batch has finished and needs one; returns whether it did."""
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
    continuations = Run.objects.filter(continuation_of_batch_id=batch_id).aggregate(
        total=Count("id"), live=Count("id", filter=~_NEVER_RAN)
    )
    if continuations["live"] or continuations["total"] >= MAX_CONTINUATION_ATTEMPTS:
        return False

    user = legs[0].user
    denial = _access_denial(user, coordinator.repo_id)
    agent_model, agent_thinking_level = pinned_agent_override(coordinator)
    try:
        with transaction.atomic():
            continuation = create_run(
                trigger_type=SessionOrigin.DELEGATION_RESUME,
                task_result_id=None,
                status=RunStatus.QUEUED,
                thread_id=coordinator.pk,
                repo_id=coordinator.repo_id,
                ref=coordinator.ref,
                user=user,
                muted=legs[0].muted,
                prompt=render_batch_summary(batch_id, legs),
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                sandbox_environment_id=str(coordinator.sandbox_environment_id)
                if coordinator.sandbox_environment_id
                else None,
                continuation_of_batch_id=batch_id,
                waits_for_id=_active_run_id(coordinator.pk),
            )
            if denial is not None:
                now = timezone.now()
                # The newest QUEUED row frees no slot when it fails, so there is nothing to dispatch.
                Run.objects.finish(
                    continuation,
                    RunStatus.FAILED,
                    skip_dispatch=True,
                    never_started=True,
                    error_message=denial,
                    finished_at=now,
                    started_at=now,
                )
                logger.warning("resume_coordinator: batch %s cannot resume %s: %s", batch_id, coordinator.pk, denial)
                return False
    except IntegrityError:
        return False
    dispatch_next_queued(coordinator.pk)
    return True


def _active_run_id(thread_id: str) -> uuid.UUID | None:
    """The run in flight on ``thread_id``, whatever started it, for its continuation to wait for."""
    return (
        Run.objects
        .filter(session_id=thread_id, status__in=[RunStatus.READY, RunStatus.RUNNING])
        .order_by("-created_at")
        .values_list("pk", flat=True)
        .first()
    )


def _access_denial(user: User | None, repo_id: str) -> str | None:
    """Why the continuation may not run as ``user`` on ``repo_id``, or ``None`` when it may."""
    if user is None:
        return f"{ACCESS_DENIED_PREFIX}: the user who delegated these sub-jobs no longer exists."
    try:
        assert_repo_access(user, [repo_id])
    except RepoAccessError as err:
        return f"{ACCESS_DENIED_PREFIX}: {err}"
    return None


def resume_stalled_batches() -> int:
    """Resume the coordinators the ``run_finished`` path left waiting; returns how many continuations it queued."""
    finished_recently = Run.objects.filter(
        trigger_type=SessionOrigin.DELEGATED_JOB, finished_at__gte=timezone.now() - STALLED_BATCH_WINDOW
    ).values("batch_id")
    resumed = Run.objects.filter(continuation_of_batch_id__isnull=False).exclude(_NEVER_RAN)
    batch_ids = (
        Run.objects
        .filter(trigger_type=SessionOrigin.DELEGATED_JOB, batch_id__in=finished_recently)
        .exclude(batch_id__in=resumed.values("continuation_of_batch_id"))
        .values("batch_id")
        .annotate(unfinished=Count("id", filter=~Q(status__in=RunStatus.terminal())))
        .filter(unfinished=0)
        .values_list("batch_id", flat=True)
    )
    queued = 0
    for batch_id in batch_ids:
        try:
            if _resume_batch(batch_id):
                queued += 1
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
    return queued


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


NEEDS_YOU = frozenset({"waiting_input"})
ACTIVE = frozenset({"queued", "waits", "running", "stopping"})
FAILED = frozenset({"failed", "lost"})

_LEG_STATES = {
    RunStatus.RUNNING: "running",
    RunStatus.WAITING_INPUT: "waiting_input",
    RunStatus.SUCCESSFUL: "finished",
    RunStatus.FAILED: "failed",
}


def leg_state(leg: Run) -> str:
    """The status of a delegated job as the transcript shows it; precedence matches ``_leg_outcome``."""
    if _stopping(leg):
        return "stopping"
    if leg.is_cancelled:
        return "cancelled"
    if leg.status == RunStatus.FAILED and leg.error_message.startswith(LINK_FAILED_PREFIX):
        return "lost"
    if leg.status == RunStatus.QUEUED and leg.waits_for_id:
        return "waits"
    return _LEG_STATES.get(leg.status, "queued")
