from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import IntegrityError
from django.db.models import Q

from asgiref.sync import async_to_sync
from jobs.tasks import run_job_task

from automation.titling.tasks import generate_batch_title_task
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signals import LINK_FAILED_PREFIX, emit_run_finished_if_terminal

if TYPE_CHECKING:
    from datetime import datetime

    from notifications.choices import NotifyOn

    from accounts.models import User
    from schedules.models import ScheduledJob

logger = logging.getLogger("daiv.sessions")

MAX_REPOS_PER_BATCH = 20


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


def validate_repo_list(raw) -> list[dict]:
    """Validate and normalize a list of ``{repo_id, ref}`` entries.

    Raises ``ValueError`` on any violation. Returns a fresh list of normalized dicts
    (guaranteed string keys/values, no duplicates, 1-20 entries).
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("At least one repository is required.")
    if len(raw) > MAX_REPOS_PER_BATCH:
        raise ValueError(f"At most {MAX_REPOS_PER_BATCH} repositories allowed per submission.")

    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry.keys()) != {"repo_id", "ref"}:
            raise ValueError("Each entry must be an object with keys 'repo_id' and 'ref'.")
        repo_id = entry["repo_id"]
        ref = entry["ref"] or ""
        if not isinstance(repo_id, str) or not repo_id.strip():
            raise ValueError("repo_id must be a non-empty string.")
        if not isinstance(ref, str):
            raise ValueError("ref must be a string (empty for default branch).")
        key = (repo_id, ref)
        if key in seen:
            label = f"{repo_id} on {ref}" if ref else repo_id
            raise ValueError(f"Repository already in the list: {label}.")
        seen.add(key)
        out.append({"repo_id": repo_id, "ref": ref})
    return out


def _validate(repos: list[RepoTarget]) -> None:
    if not repos:
        raise ValueError("repos must contain at least one entry")
    if len(repos) > MAX_REPOS_PER_BATCH:
        raise ValueError(f"repos exceeds the maximum of {MAX_REPOS_PER_BATCH}")


async def aget_or_create_session(
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
) -> Session:
    """Idempotent session bootstrap keyed on thread_id. First caller sets origin
    and context; later callers just bump last_active_at (a webhook session later
    continued via API keeps origin=issue_webhook).
    """
    session, created = await Session.objects.aget_or_create(
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
        },
    )
    if not created:
        await session.atouch()
    return session


async def acreate_run(
    *,
    trigger_type: str,
    task_result_id: uuid.UUID | None,
    repo_id: str,
    ref: str = "",
    prompt: str = "",
    agent_model: str = "",
    agent_thinking_level: str = "",
    use_max: bool = False,
    issue_iid: int | None = None,
    merge_request_iid: int | None = None,
    mention_comment_id: str = "",
    scheduled_job: ScheduledJob | None = None,
    user: User | None = None,
    external_username: str = "",
    notify_on: NotifyOn | None = None,
    batch_id: uuid.UUID | None = None,
    thread_id: str | None = None,
    title: str = "",
    sandbox_environment_id: str | None = None,
    status: str = RunStatus.READY,
) -> Run:
    """Async: create a Session (idempotent) then a Run linked to it.

    ``use_max=True`` pins the site-configured max agent model/thinking level (used by
    the ``daiv-max`` webhook label) unless an explicit ``agent_model`` was supplied.
    """
    if use_max and not agent_model:
        from core.site_settings import site_settings

        agent_model = site_settings.agent_max_model_name
        agent_thinking_level = site_settings.agent_max_thinking_level
    effective_thread_id = thread_id or str(uuid.uuid4())
    session = await aget_or_create_session(
        thread_id=effective_thread_id,
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
    )
    return await Run.objects.acreate(
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
        notify_on=notify_on,
        batch_id=batch_id,
        title=title[: Run._meta.get_field("title").max_length],
        sandbox_environment_id=sandbox_environment_id,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention_comment_id,
    )


async def _mark_failed_and_advance(run: Run, *, prefix: str, err: Exception, previous_status: str) -> None:
    """Transition a row to FAILED with finished_at and emit ``run_finished``.

    Does NOT touch ``Session.active_run_id`` (that is ``SessionLock``'s job); the
    "advance" is emitting ``run_finished`` so any QUEUED siblings on the session
    get dispatched. Used by the services-layer post-create error paths (enqueue or
    task-result-id-link failure). The emit is best-effort — if it raises, we log
    loudly and recommend the operator run ``release_orphan_queued_sessions`` to
    recover stranded siblings.
    """
    update_fields = run.mark_failed(prefix, err)
    try:
        await run.asave(update_fields=update_fields)
    except Exception:
        # Best-effort: on a failed save the row may be left non-terminal (still READY)
        # while we still emit below to advance siblings. release_orphan_queued_sessions
        # reconciles any row stranded this way.
        logger.exception("submit_batch_runs: terminal save failed for run=%s", run.pk)
    try:
        await asyncio.to_thread(emit_run_finished_if_terminal, run, previous_status=previous_status)
    except Exception:
        logger.exception(
            "submit_batch_runs: emit_run_finished_if_terminal failed for run=%s; "
            "queued siblings on this session may be stranded — run release_orphan_queued_sessions",
            run.pk,
        )


async def asubmit_batch_runs(
    *,
    user: User | None,
    prompt: str,
    repos: list[RepoTarget],
    agent_model: str = "",
    agent_thinking_level: str = "",
    notify_on: NotifyOn | None = None,
    trigger_type: str,
    scheduled_job: ScheduledJob | None = None,
    external_username: str = "",
    thread_id: str | None = None,
) -> BatchSubmitResult:
    """Enqueue N ``run_job_task`` instances sharing a ``batch_id``; record N ``Run`` rows.

    Each ``RepoTarget`` carries its own ``sandbox_environment_id`` (resolved upstream by
    :func:`sandbox_envs.services.resolve_repo_envs`), so the batch can mix per-repo envs.

    Best-effort: any per-repo exception (enqueue failure or post-enqueue run-creation
    failure) lands in ``result.failed`` while siblings continue.
    """
    _validate(repos)
    if thread_id is not None:
        if not thread_id:
            raise ValueError("thread_id must be a non-empty UUID string")
        try:
            uuid.UUID(thread_id)
        except (ValueError, TypeError) as err:
            raise ValueError("thread_id must be a UUID string") from err
        if len(repos) != 1:
            raise ValueError("thread_id continuation requires exactly one repo")
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
            "notify_on": notify_on,
            "batch_id": batch_id,
            "thread_id": effective_thread_id,
            "title": run_title,
            "sandbox_environment_id": target.sandbox_environment_id,
        }

        # Claim the session atomically by trying to create a READY row. The partial
        # unique constraint ``run_one_active_per_session`` raises IntegrityError
        # when a sibling (READY/RUNNING) is already active on this session — in that
        # case we fall back to QUEUED, no task enqueue.
        try:
            run = await acreate_run(**common_kwargs, task_result_id=None, status=RunStatus.READY)
        except IntegrityError:
            try:
                return await acreate_run(**common_kwargs, task_result_id=None, status=RunStatus.QUEUED)
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

        try:
            task = await run_job_task.aenqueue(
                repo_id=target.repo_id,
                prompt=prompt,
                ref=target.ref or None,
                agent_model=agent_model or None,
                agent_thinking_level=agent_thinking_level or None,
                thread_id=effective_thread_id,
                sandbox_environment_id=target.sandbox_environment_id,
                run_id=str(run.pk),
            )
        except Exception as err:  # noqa: BLE001
            logger.exception("submit_batch_runs: enqueue failed for repo_id=%s batch_id=%s", target.repo_id, batch_id)
            await _mark_failed_and_advance(run, prefix="enqueue_failed", err=err, previous_status=RunStatus.READY)
            return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error=f"{type(err).__name__}: {err}")

        try:
            run.task_result_id = task.id
            await run.asave(update_fields=["task_result_id"])
        except Exception as save_err:
            # The broker now holds a task this Run row doesn't link to.
            # Mark the row FAILED so callers see the failure and queued siblings advance;
            # the orphan task itself will execute and ``_sync_run_for_task`` will no-op
            # (no Run with that task_result_id).
            logger.exception(
                "submit_batch_runs: failed to link task_result_id=%s to run=%s (orphan task will run)", task.id, run.pk
            )
            # Surface in error_message that the agent may run to completion (push a
            # commit / open an MR) while this row shows FAILED — the work is real but
            # uncapturable because nothing links back to it.
            await _mark_failed_and_advance(
                run, prefix=LINK_FAILED_PREFIX, err=save_err, previous_status=RunStatus.READY
            )
            return BatchSubmitFailure(
                repo_id=target.repo_id, ref=target.ref, error=f"LinkFailed: {type(save_err).__name__}: {save_err}"
            )
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
