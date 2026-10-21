"""Delegated jobs: a coordinator run fans tailored sub-jobs (legs) out to other repositories with ``delegate_jobs``,
ends its turn, and is resumed once every leg has finished.

:class:`SessionJobDelegator` is the agent's ``automation.agent.delegation.JobDelegator`` for executor runs. It submits
the legs as one batch of ``SessionOrigin.DELEGATED_JOB`` runs, each in a new session whose ``parent`` is the
coordinator's. Delegation is one level deep: a session with a parent cannot delegate, so a leg's first run is its
whole answer and the batch is done when every leg's run is.

:func:`resume_coordinator` runs on every leg's ``run_finished``. Once the whole batch is terminal it queues one run on
the coordinator's session (``continuation_of_batch_id``) whose prompt summarises every leg, and releases it like any
QUEUED run: a coordinator run still in flight keeps it QUEUED until that run finishes, and the orphan sweep recovers
one whose release was lost. When several legs finish at once, ``run_one_continuation_per_batch`` lets one insert win.
A batch none of whose legs reached the task queue resumes nothing: the tool already told the agent nothing started.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.urls import reverse

from asgiref.sync import sync_to_async

from automation.agent.delegation import MAX_DELEGATED_TARGETS, DelegationError, DelegationResult, FailedLeg, StartedLeg
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryCatalog
from core.utils import build_absolute_url
from sessions.admission import AdmissionError, RepoAccessError, aadmit, aassert_repo_access
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.queue import dispatch_next_queued
from sessions.services import RepoTarget, asubmit_batch_runs, create_run

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from automation.agent.delegation import DelegatedTarget

logger = logging.getLogger("daiv.sessions")

LEG_REPLY_MAX_CHARS = 1000

_OUTCOMES = {
    RunStatus.SUCCESSFUL: "succeeded",
    RunStatus.FAILED: "failed",
    RunStatus.WAITING_INPUT: "stopped to ask a question",
}


class SessionJobDelegator:
    """Submits ``delegate_jobs`` legs as the coordinator session's user."""

    async def adelegate(self, *, thread_id: str, goal: str, targets: Sequence[DelegatedTarget]) -> DelegationResult:
        session = await Session.objects.select_related("user").filter(pk=thread_id).afirst()
        if session is None:
            raise DelegationError("This run has no session to resume, so it cannot delegate.")
        if session.parent_id is not None:
            raise DelegationError(
                "This session was itself delegated, and delegated sessions cannot delegate further: do the work here "
                "or report what else is needed in your final response."
            )
        if session.user is None:
            raise DelegationError(
                "Sub-jobs run as the DAIV user behind this session, and it has none, so it cannot delegate."
            )
        if not goal:
            raise DelegationError("`goal` must name the overall objective.")
        if not targets:
            raise DelegationError("Give at least one target.")
        if len(targets) > MAX_DELEGATED_TARGETS:
            raise DelegationError(f"At most {MAX_DELEGATED_TARGETS} targets per call; split the work across calls.")
        if any(not target.repo_id or not target.prompt for target in targets):
            raise DelegationError("Every target needs a `repo_id` and a prompt of its own.")
        await _acheck_checkouts(session, targets)

        user = session.user
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
            admission = await aadmit(
                user=user,
                repos=[RepoTarget(repo_id=t.repo_id, ref=t.ref, prompt=t.prompt) for t in allowed],
                agent_model=None,
                agent_thinking_level=None,
                environment=None,
            )
            result = await asubmit_batch_runs(
                user=user,
                prompt=goal,
                repos=admission.targets,
                agent_model=admission.agent_model,
                agent_thinking_level=admission.agent_thinking_level,
                trigger_type=SessionOrigin.DELEGATED_JOB,
                parent=session,
            )
        except AdmissionError as err:
            raise DelegationError(str(err)) from err

        session_urls = await sync_to_async(lambda: [_session_url(run.session_id) for run in result.runs])()
        return DelegationResult(
            batch_id=str(result.batch_id),
            started=[
                StartedLeg(repo_id=run.repo_id, ref=run.ref, thread_id=str(run.session_id), session_url=url)
                for run, url in zip(result.runs, session_urls, strict=True)
            ],
            failed=[*failed, *(FailedLeg(f.repo_id, f.ref, f.error) for f in result.failed)],
        )


async def _acheck_checkouts(session: Session, targets: Sequence[DelegatedTarget]) -> None:
    """Refuse a target on the coordinator's own checkout or a checkout listed twice; a blank ref and the default
    branch's name are the same checkout."""
    slugs = {session.repo_id, *(t.repo_id for t in targets)}
    default_branches = {
        slug: branch
        async for slug, branch in RepositoryCatalog.objects.filter(
            provider=codebase_settings.CLIENT.value, slug__in=slugs
        ).values_list("slug", "default_branch")
    }

    def checkout(repo_id: str, ref: str) -> tuple[str, str]:
        return repo_id, ref or default_branches.get(repo_id, "")

    own = checkout(session.repo_id, session.ref)
    seen: set[tuple[str, str]] = set()
    for target in targets:
        key = checkout(target.repo_id, target.ref)
        where = f"{target.repo_id} on {target.ref or 'its default branch'}"
        if key == own:
            raise DelegationError(
                f"{where} is this session's own checkout; use the `task` tool (subagents) for parallel work here."
            )
        if key in seen:
            raise DelegationError(f"{where} is listed twice; combine its work into one prompt.")
        seen.add(key)


def _session_url(thread_id: str) -> str:
    return build_absolute_url(reverse("session_detail", kwargs={"thread_id": thread_id}))


def resume_coordinator(run: Run) -> None:
    """Queue the coordinator's continuation once ``run``'s delegated batch has finished; the module docstring says
    when nothing is queued."""
    if run.trigger_type != SessionOrigin.DELEGATED_JOB or run.batch_id is None:
        return
    legs = list(Run.objects.by_batch(run.batch_id).select_related("session__parent__user").order_by("created_at", "id"))
    if any(leg.status not in RunStatus.terminal() for leg in legs) or not any(leg.task_result_id for leg in legs):
        return
    coordinator = legs[0].session.parent
    if coordinator is None:
        logger.info("resume_coordinator: batch %s has no coordinator session left to resume", run.batch_id)
        return
    if Run.objects.filter(continuation_of_batch_id=run.batch_id).exists():
        return

    try:
        with transaction.atomic():
            create_run(
                trigger_type=SessionOrigin.DELEGATED_JOB,
                task_result_id=None,
                status=RunStatus.QUEUED,
                thread_id=coordinator.pk,
                repo_id=coordinator.repo_id,
                ref=coordinator.ref,
                user=coordinator.user,
                prompt=render_batch_summary(run.batch_id, legs),
                agent_model=coordinator.agent_model,
                agent_thinking_level=coordinator.agent_thinking_level,
                sandbox_environment_id=str(coordinator.sandbox_environment_id)
                if coordinator.sandbox_environment_id
                else None,
                continuation_of_batch_id=run.batch_id,
            )
    except IntegrityError:
        return
    dispatch_next_queued(coordinator.pk)


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
