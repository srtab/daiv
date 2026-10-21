"""Delegated jobs: a coordinator run fans tailored sub-jobs (legs) out to other repositories with ``delegate_jobs``,
ends its turn, and is resumed once every leg has finished.

:class:`SessionJobDelegator` is the agent's ``automation.agent.delegation.JobDelegator`` for executor runs, and
:func:`adelegator_for` hands one only to a session that may delegate. It submits the legs as one batch of
``SessionOrigin.DELEGATED_JOB`` runs as the user who triggered the delegating run, each in a new session whose
``parent`` is the coordinator's. Delegation is one level deep: a session with a parent gets no delegator, so a leg's
first run is its whole answer and the batch is done when every leg's run is.

:func:`resume_coordinator` runs on every leg's ``run_finished``. Once the whole batch is terminal it queues one
``SessionOrigin.DELEGATION_RESUME`` run on the coordinator's session (``continuation_of_batch_id``), as the legs'
user, whose prompt summarises every leg, and releases it like any QUEUED run, so it waits its turn behind a
coordinator run still in flight. When several legs finish at once, ``run_one_continuation_per_batch`` lets one insert
win. A user who has lost WRITE access to the coordinator's repository meanwhile gets the continuation FAILED instead.
A batch none of whose legs reached the task queue resumes nothing: the tool already told the agent nothing started.

:func:`resume_stalled_batches` is the periodic backstop: it resumes a finished batch the signal path left without a
continuation, retries one whose continuation never reached the task queue (up to ``MAX_CONTINUATION_ATTEMPTS``
continuations per batch), and releases a continuation left QUEUED on a coordinator with no active run.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from asgiref.sync import sync_to_async
from sandbox_envs.selection import aresolve_repo_envs

from automation.agent.delegation import MAX_DELEGATED_TARGETS, DelegationError, DelegationResult, FailedLeg, StartedLeg
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE
from codebase.repo_config import RepositoryConfig
from core.utils import build_absolute_url
from sessions.admission import (
    AdmissionError,
    RepoAccessError,
    aassert_repo_access,
    assert_repo_access,
    check_agent_override,
)
from sessions.artifacts import aresolve_active_run
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.queue import ENQUEUE_FAILED_PREFIX, dispatch_next_queued, release_orphan_queued_runs
from sessions.services import RepoTarget, asubmit_batch_runs, create_run, pinned_agent_override

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from accounts.models import User
    from automation.agent.delegation import Checkout, DelegatedTarget

logger = logging.getLogger("daiv.sessions")

LEG_REPLY_MAX_CHARS = 1000
MAX_CONTINUATION_ATTEMPTS = 3
STALLED_BATCH_WINDOW = timedelta(days=1)
ACCESS_DENIED_PREFIX = "access_denied"

_OUTCOMES = {
    RunStatus.SUCCESSFUL: "succeeded",
    RunStatus.FAILED: "failed",
    RunStatus.WAITING_INPUT: "stopped to ask a question",
}
_NEVER_QUEUED = Q(status=RunStatus.FAILED, error_message__startswith=ENQUEUE_FAILED_PREFIX)


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
        await _acheck_checkouts(checkout, targets)

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
            agent_model, agent_thinking_level = check_agent_override(None, None)
            repos = await aresolve_repo_envs(
                user=user,
                repos=[RepoTarget(repo_id=t.repo_id, ref=t.ref, prompt=t.prompt) for t in allowed],
                explicit_env_id=None,
            )
            result = await asubmit_batch_runs(
                user=user,
                prompt=goal,
                repos=repos,
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                trigger_type=SessionOrigin.DELEGATED_JOB,
                parent=run.session,
            )
        except AdmissionError as err:
            raise DelegationError(str(err)) from err

        started = await sync_to_async(
            lambda: [
                StartedLeg(leg.repo_id, leg.ref, str(leg.session_id), _session_url(leg.session_id))
                for leg in result.runs
            ]
        )()
        return DelegationResult(
            batch_id=str(result.batch_id),
            started=started,
            failed=[*failed, *(FailedLeg(f.repo_id, f.ref, f.error) for f in result.failed)],
        )


async def _aactive_run(thread_id: str) -> Run | None:
    if (run := await aresolve_active_run(thread_id)) is None:
        return None
    return await Run.objects.select_related("session", "user").aget(pk=run.pk)


async def _acheck_checkouts(own: Checkout, targets: Sequence[DelegatedTarget]) -> None:
    """Refuse a target on the delegating run's own checkout or a checkout listed twice. A blank ref is the
    repository's default branch, and slugs compare case-insensitively, as the platforms resolve them."""
    own_slug = own.repo_id.casefold()
    refs: defaultdict[str, set[str]] = defaultdict(set)
    for target in targets:
        refs[target.repo_id.casefold()].add(target.ref)
    default_branches = {own_slug: own.default_branch}
    for target in targets:
        slug = target.repo_id.casefold()
        if slug not in default_branches and "" in refs[slug] and len(refs[slug]) > 1:
            default_branches[slug] = await _adefault_branch(target.repo_id)

    def checkout(repo_id: str, ref: str) -> tuple[str, str]:
        slug = repo_id.casefold()
        return slug, ref or default_branches.get(slug, "")

    own_checkout = checkout(own.repo_id, own.ref)
    seen: set[tuple[str, str]] = set()
    for target in targets:
        key = checkout(target.repo_id, target.ref)
        where = f"{target.repo_id} on {target.ref or 'its default branch'}"
        if key == own_checkout:
            raise DelegationError(
                f"{where} is this session's own checkout; use the `task` tool (subagents) for parallel work here."
            )
        if key in seen:
            raise DelegationError(f"{where} is listed twice; combine its work into one prompt.")
        seen.add(key)


async def _adefault_branch(repo_id: str) -> str:
    """The branch a blank ref clones on ``repo_id``, or ``""`` when it cannot be read; that leg then fails alone."""
    try:
        config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
    except Exception:  # noqa: BLE001
        logger.warning("delegate_jobs: could not read the default branch of %s", repo_id, exc_info=True)
        return ""
    return config.default_branch or ""


def _session_url(thread_id: str) -> str:
    return build_absolute_url(reverse("session_detail", kwargs={"thread_id": thread_id}))


def resume_coordinator(run: Run) -> None:
    """Queue the coordinator's continuation once ``run``'s delegated batch has finished; the module docstring says
    when nothing is queued."""
    if run.trigger_type != SessionOrigin.DELEGATED_JOB or run.batch_id is None:
        return
    _resume_batch(run.batch_id)


def _resume_batch(batch_id: uuid.UUID) -> bool:
    """Queue ``batch_id``'s continuation if the batch has finished and needs one; returns whether it did."""
    legs = list(Run.objects.by_batch(batch_id).select_related("user", "session__parent").order_by("created_at", "id"))
    if any(leg.status not in RunStatus.terminal() for leg in legs) or not any(leg.task_result_id for leg in legs):
        return False
    coordinator = legs[0].session.parent
    if coordinator is None:
        logger.info("resume_coordinator: batch %s has no coordinator session left to resume", batch_id)
        return False
    continuations = Run.objects.filter(continuation_of_batch_id=batch_id).aggregate(
        total=Count("id"), live=Count("id", filter=~_NEVER_QUEUED)
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
                prompt=render_batch_summary(batch_id, legs),
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                sandbox_environment_id=str(coordinator.sandbox_environment_id)
                if coordinator.sandbox_environment_id
                else None,
                continuation_of_batch_id=batch_id,
            )
            if denial is not None:
                now = timezone.now()
                Run.objects.finish(
                    continuation,
                    RunStatus.FAILED,
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
    resumed = Run.objects.filter(continuation_of_batch_id__isnull=False).exclude(_NEVER_QUEUED)
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

    waiting = Run.objects.filter(status=RunStatus.QUEUED, continuation_of_batch_id__isnull=False)
    try:
        release_orphan_queued_runs(session_ids=waiting.values("session_id"))
    except Exception:
        logger.exception("resume_stalled_batches: failed to release the queued continuations")
    return queued


def render_batch_summary(batch_id: uuid.UUID, legs: Sequence[Run]) -> str:
    """The continuation's prompt: each leg's outcome, session, merge request and reply."""
    succeeded = sum(leg.status == RunStatus.SUCCESSFUL for leg in legs)
    lines = [f"The sub-jobs you delegated (batch {batch_id}) have finished: {succeeded} of {len(legs)} succeeded.", ""]
    for leg in legs:
        where = f"{leg.repo_id} on {leg.ref}" if leg.ref else leg.repo_id
        lines.append(f"## {where}: {_OUTCOMES.get(leg.status, leg.status)}")
        lines.append(f"- Session: {_session_url(leg.session_id)}")
        if leg.merge_request_web_url:
            lines.append(f"- Merge request: {leg.merge_request_web_url}")
        if reply := (leg.result_summary or leg.error_message).strip():
            # Quoted so a leg's reply can't pass for a sibling's section or for these instructions.
            lines.append("- Reply:")
            lines.extend(f"  > {line}" for line in reply[:LEG_REPLY_MAX_CHARS].splitlines())
        elif leg.status == RunStatus.FAILED:
            lines.append("- Reply: none; it failed without an error message, so open its session to see why.")
        lines.append("")
    lines.append(
        "Check the outcome and report it where the request came from. Delegate another batch if follow-up work "
        "depends on these results."
    )
    return "\n".join(lines)
