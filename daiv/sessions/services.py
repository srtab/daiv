from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Q

from asgiref.sync import async_to_sync, sync_to_async

from codebase.authorization import aassert_can_run
from codebase.references import MAX_REFS_PER_SUBMISSION, merge_stored_refs
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.queue import aenqueue_run, amark_failed_and_advance
from sessions.repo_state import mr_to_payload
from sessions.tasks import generate_batch_title_task
from sessions.validators import MAX_REPOS_PER_BATCH

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from accounts.models import User
    from codebase.base import MergeRequest
    from codebase.references import RefIn
    from schedules.models import ScheduledJob

logger = logging.getLogger("daiv.sessions")


@dataclass(frozen=True)
class RepoTarget:
    repo_id: str
    ref: str = ""
    sandbox_environment_id: str | None = None


@dataclass(frozen=True)
class BatchSubmitFailure:
    repo_id: str
    ref: str
    error: str


@dataclass(frozen=True)
class BatchSubmitResult:
    batch_id: uuid.UUID
    runs: list[Run] = field(default_factory=list)
    failed: list[BatchSubmitFailure] = field(default_factory=list)


def _validate(repos: list[RepoTarget]) -> None:
    if not repos:
        raise ValueError("repos must contain at least one entry")
    if len(repos) > MAX_REPOS_PER_BATCH:
        raise ValueError(f"repos exceeds the maximum of {MAX_REPOS_PER_BATCH}")


def get_or_create_session(
    *,
    thread_id: str,
    origin: str,
    repo_id: str,
    ref: str = "",
    user=None,
    external_username: str = "",
    title: str = "",
    agent_model: str = "",
    agent_thinking_level: str = "",
    sandbox_environment_id: str | None = None,
    scheduled_job=None,
    issue_iid: int | None = None,
    merge_request_iid: int | None = None,
    mcp_overrides: dict | None = None,
) -> Session:
    """Idempotent session bootstrap keyed on thread_id. First caller sets origin
    and context; later callers just bump last_active_at (a webhook session later
    continued via API keeps origin=issue_webhook). External refs never land here —
    :func:`_amerge_session_refs` writes them only after a run row exists.
    """
    session, created = Session.objects.get_or_create(
        thread_id=thread_id,
        defaults={
            "origin": origin,
            "repo_id": repo_id,
            "ref": ref,
            "user": user,
            "external_username": external_username,
            "title": title[: Session._meta.get_field("title").max_length],
            "agent_model": agent_model,
            "agent_thinking_level": agent_thinking_level,
            "sandbox_environment_id": sandbox_environment_id,
            "scheduled_job": scheduled_job,
            "issue_iid": issue_iid,
            "merge_request_iid": merge_request_iid,
            "mcp_overrides": mcp_overrides or {},
        },
    )
    if not created:
        session.touch()
    return session


async def _amerge_session_refs(session: Session, external_refs: list[dict]) -> None:
    """Merge newly declared refs into the session row under a row lock, re-reading the column
    inside the transaction so concurrent continuations can't clobber each other's refs.
    (SQLite ignores FOR UPDATE; on Postgres the lock is real.)

    Called only after the submission's run row exists — a failed claim leaves no refs behind —
    and before enqueue, so an enqueue failure can still leave them: accepted, since the caller
    is told the submission failed and a resubmit re-declares them.
    """

    def _merge_locked() -> list[dict]:
        with transaction.atomic():
            current = Session.objects.select_for_update().only("external_refs").get(pk=session.pk).external_refs
            merged = merge_stored_refs(current, external_refs)
            if merged != current:
                Session.objects.filter(pk=session.pk).update(external_refs=merged)
            return merged

    session.external_refs = await sync_to_async(_merge_locked)()


async def apersist_session_ref(
    *, thread_id: str, current_ref: str, merge_request: MergeRequest | dict | None, published: bool
) -> None:
    """Sync ``Session.ref`` with the branch the agent is working on.

    ``Session.ref`` is the branch a session is *working on*, not the one it started from —
    that stays on every ``Run.ref``, which nothing rewrites — so the composer pill and the
    session list keep agreeing with the merge request beside them. ``current_ref`` is the branch
    the clone landed on, ``""`` when it landed on a tag or a commit.

    A blank ref is filled only when the branch is known: a merge request that didn't revive stays an
    ``lc:2`` envelope with no top-level ``source_branch``, and a publish can land before its merge
    request opens, so either leaves it alone.
    """
    branch = (mr_to_payload(merge_request) or {}).get("source_branch")
    if isinstance(branch, str) and branch and branch != current_ref:
        await Session.objects.filter(thread_id=thread_id).aupdate(ref=branch)
    elif current_ref and (branch == current_ref or (merge_request is None and not published)):
        await Session.objects.filter(thread_id=thread_id, ref="").aupdate(ref=current_ref)


async def aget_session_ref(*, thread_id: str) -> str:
    """The branch a session is working on, or ``""`` when it has none (or no such session).

    The read side of :func:`apersist_session_ref`, for a webhook re-trigger that carries no ref of
    its own — an issue is not a branch.
    """
    return await Session.objects.filter(thread_id=thread_id).values_list("ref", flat=True).afirst() or ""


async def aget_task_run_id(task_result_id: uuid.UUID | str) -> str | None:
    """The id of the ``Run`` linked to a task result, or ``None`` when none is.

    A webhook callback commits the Run with its task (see ``webhooks.dispatch``), so a worker that sees the task
    sees its Run too.
    """
    run_id = await Run.objects.filter(task_result_id=task_result_id).values_list("pk", flat=True).afirst()
    if run_id is None:
        logger.warning("No run is linked to task result %s; running without one", task_result_id)
        return None
    return str(run_id)


async def areset_session_ref(*, thread_id: str, new_ref: str) -> None:
    """Re-pin ``Session.ref`` after a run fell back off a vanished branch.

    Unlike :func:`apersist_session_ref` (success-only, driven by the run's outcome), this
    fires the moment the clone falls back, so the session self-heals even if the turn fails.
    """
    await Session.objects.filter(thread_id=thread_id).aupdate(ref=new_ref)


def create_run(
    *,
    trigger_type: str,
    task_result_id: uuid.UUID | None,
    repo_id: str,
    ref: str = "",
    prompt: str = "",
    agent_model: str = "",
    agent_thinking_level: str = "",
    issue_iid: int | None = None,
    merge_request_iid: int | None = None,
    mention_comment_id: str = "",
    scheduled_job: ScheduledJob | None = None,
    user: User | None = None,
    external_username: str = "",
    muted: bool | None = None,
    batch_id: uuid.UUID | None = None,
    thread_id: str | None = None,
    title: str = "",
    sandbox_environment_id: str | None = None,
    status: str = RunStatus.READY,
    mcp_overrides: dict | None = None,
    session: Session | None = None,
) -> Run:
    """Create a Session (idempotent) then a Run linked to it.

    ``session`` is one the caller already opened: the Run joins it, and the arguments that only seed a new
    session (``thread_id``, ``issue_iid``, ``scheduled_job``, ``mcp_overrides``) go unused.
    """
    if session is None:
        session = get_or_create_session(
            thread_id=thread_id or str(uuid.uuid4()),
            origin=trigger_type,
            repo_id=repo_id,
            ref=ref,
            user=user,
            external_username=external_username,
            title=title,
            agent_model=agent_model,
            agent_thinking_level=agent_thinking_level,
            sandbox_environment_id=sandbox_environment_id,
            scheduled_job=scheduled_job,
            issue_iid=issue_iid,
            merge_request_iid=merge_request_iid,
            mcp_overrides=mcp_overrides,
        )
    return Run.objects.create(
        session=session,
        trigger_type=trigger_type,
        status=status,
        task_result_id=task_result_id,
        user=user,
        external_username=external_username,
        repo_id=repo_id,
        ref=ref,
        prompt=prompt,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
        muted=muted,
        batch_id=batch_id,
        title=title[: Run._meta.get_field("title").max_length],
        sandbox_environment_id=sandbox_environment_id,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention_comment_id,
    )


acreate_run = sync_to_async(create_run)


async def asubmit_batch_runs(
    *,
    user: User | None,
    prompt: str,
    repos: list[RepoTarget],
    agent_model: str = "",
    agent_thinking_level: str = "",
    muted: bool | None = None,
    trigger_type: str,
    scheduled_job: ScheduledJob | None = None,
    external_username: str = "",
    thread_id: str | None = None,
    mcp_overrides: dict | None = None,
    references: Sequence[RefIn] | None = None,
) -> BatchSubmitResult:
    """Enqueue N ``run_job_task`` instances sharing a ``batch_id``; record N ``Run`` rows.

    Each ``RepoTarget`` carries its own ``sandbox_environment_id`` (resolved upstream by
    :func:`sandbox_envs.selection.aresolve_repo_envs` or its sync wrapper), so the batch can mix per-repo envs.

    Best-effort: a per-repo failure (run creation, refs merge, enqueue or link) lands in
    ``result.failed`` while siblings continue.
    """
    _validate(repos)
    if user is not None:
        # Gate only when a user is attached. All current callers supply one; the None branch
        # is a defensive allowance for future internal callers with no user context.
        await aassert_can_run(user, [target.repo_id for target in repos])
    if thread_id is not None:
        if not thread_id:
            raise ValueError("thread_id must be a non-empty UUID string")
        try:
            uuid.UUID(thread_id)
        except (ValueError, TypeError) as err:
            raise ValueError("thread_id must be a UUID string") from err
        if len(repos) != 1:
            raise ValueError("thread_id continuation requires exactly one repo")
    stored_refs = [ref.to_stored() for ref in references or []]
    if len(stored_refs) > MAX_REFS_PER_SUBMISSION:
        raise ValueError(f"references exceeds the maximum of {MAX_REFS_PER_SUBMISSION}")
    batch_id = uuid.uuid4()

    schedule_run_base = 0
    if trigger_type == SessionOrigin.SCHEDULE and scheduled_job is not None:
        schedule_run_base = await Run.objects.filter(session__scheduled_job=scheduled_job).acount()

    async def _submit_one(idx: int, target: RepoTarget) -> Run | BatchSubmitFailure:
        effective_thread_id = thread_id or str(uuid.uuid4())

        run_title = ""
        if trigger_type == SessionOrigin.SCHEDULE and scheduled_job is not None:
            run_title = f"{scheduled_job.name} · run #{schedule_run_base + idx + 1}"

        common_kwargs: dict = {
            "trigger_type": trigger_type,
            "repo_id": target.repo_id,
            "ref": target.ref,
            "prompt": prompt,
            "agent_model": agent_model,
            "agent_thinking_level": agent_thinking_level,
            "scheduled_job": scheduled_job,
            "user": user,
            "external_username": external_username,
            "muted": muted,
            "batch_id": batch_id,
            "thread_id": effective_thread_id,
            "title": run_title,
            "sandbox_environment_id": target.sandbox_environment_id,
            "mcp_overrides": mcp_overrides,
        }

        # Claim the session atomically by trying to create a READY row. The partial
        # unique constraint ``run_one_active_per_session`` raises IntegrityError
        # when a sibling (READY/RUNNING) is already active on this session — in that
        # case we fall back to QUEUED, no task enqueue.
        try:
            run = await acreate_run(**common_kwargs, task_result_id=None, status=RunStatus.READY)
        except IntegrityError:
            try:
                run = await acreate_run(**common_kwargs, task_result_id=None, status=RunStatus.QUEUED)
            except Exception as inner_err:
                logger.exception("submit_batch_runs: queued run creation failed for repo_id=%s", target.repo_id)
                return BatchSubmitFailure(
                    repo_id=target.repo_id,
                    ref=target.ref,
                    error=f"RunCreationFailed: {type(inner_err).__name__}: {inner_err}",
                )
        except Exception as err:
            logger.exception("submit_batch_runs: run creation failed for repo_id=%s", target.repo_id)
            return BatchSubmitFailure(
                repo_id=target.repo_id, ref=target.ref, error=f"RunCreationFailed: {type(err).__name__}: {err}"
            )

        if stored_refs:
            try:
                await _amerge_session_refs(run.session, stored_refs)
            except Exception as err:  # noqa: BLE001
                # A READY row left behind would hold the one-active-per-session claim forever
                # (never enqueued → run_finished never fires → siblings stay QUEUED).
                logger.exception("submit_batch_runs: session ref merge failed for repo_id=%s", target.repo_id)
                await amark_failed_and_advance(
                    run, prefix="refs_merge_failed", err=err, log_context="submit_batch_runs"
                )
                return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error=run.error_message)

        if run.status == RunStatus.QUEUED:
            return run

        if await aenqueue_run(run, log_context="submit_batch_runs") is not None:
            return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error=run.error_message)
        return run

    # return_exceptions=True guards against BaseException (CancelledError, etc.) aborting the
    # whole batch; _submit_one already catches Exception itself.
    outcomes = await asyncio.gather(*[_submit_one(i, t) for i, t in enumerate(repos)], return_exceptions=True)

    runs: list[Run] = []
    failed: list[BatchSubmitFailure] = []
    for target, outcome in zip(repos, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.error("submit_batch_runs: unexpected exception for repo_id=%s", target.repo_id, exc_info=outcome)
            failed.append(
                BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error=f"{type(outcome).__name__}: {outcome}")
            )
        elif isinstance(outcome, BatchSubmitFailure):
            failed.append(outcome)
        else:
            runs.append(outcome)

    if runs and trigger_type in SessionOrigin.prompt_driven() and prompt:
        try:
            await generate_batch_title_task.aenqueue(batch_id=str(batch_id), prompt=prompt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to enqueue batch title task for batch_id=%s user=%s trigger=%s runs=%d",
                batch_id,
                user.pk if user is not None else None,
                trigger_type,
                len(runs),
            )

    return BatchSubmitResult(batch_id=batch_id, runs=runs, failed=failed)


def submit_batch_runs(**kwargs) -> BatchSubmitResult:
    """Sync wrapper around :func:`asubmit_batch_runs` for cron and sync views."""
    return async_to_sync(asubmit_batch_runs)(**kwargs)


async def alist_user_runs(
    user,
    *,
    repo_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    before: tuple[datetime, uuid.UUID] | None = None,
) -> list[Run]:
    """Return ``user``'s runs, newest first, optionally filtered by repo/status.

    Capped at ``limit`` rows. Callers needing truncation/pagination should pass
    ``limit + 1`` and trim. ``before`` is a keyset cursor ``(created_at, id)`` of the
    last row already seen; only rows strictly older (in ``-created_at, -id`` order) are
    returned, so pagination is stable even as new rows arrive at the head. The ``id``
    tie-break is required because a batch submit stamps several rows with the same
    ``created_at``. Backed by ``run_user_created_idx`` (user, -created_at).
    """
    qs = Run.objects.filter(user=user)
    if repo_id:
        qs = qs.filter(repo_id=repo_id)
    if status:
        qs = qs.filter(status=status)
    if before is not None:
        created_at, last_id = before
        qs = qs.filter(Q(created_at__lt=created_at) | Q(created_at=created_at, id__lt=last_id))
    return [run async for run in qs.order_by("-created_at", "-id")[:limit]]
