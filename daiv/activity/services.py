from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import IntegrityError
from django.utils import timezone

from asgiref.sync import async_to_sync
from jobs.tasks import run_job_task

from activity.models import Activity, ActivityStatus, TriggerType
from activity.signals import emit_activity_finished_if_terminal
from automation.titling.tasks import generate_batch_title_task

_PROMPT_DRIVEN = {TriggerType.API_JOB, TriggerType.MCP_JOB, TriggerType.UI_JOB}

if TYPE_CHECKING:
    from notifications.choices import NotifyOn
    from sandbox_envs.models import SandboxEnvironment

    from accounts.models import User
    from schedules.models import ScheduledJob

logger = logging.getLogger("daiv.activity")

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
    activities: list[Activity] = field(default_factory=list)
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


def create_activity(
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
    sandbox_environment: SandboxEnvironment | None = None,
    status: str = ActivityStatus.READY,
) -> Activity:
    """Create an Activity record linked to a DBTaskResult.

    ``notify_on=None`` defers to ``Activity.effective_notify_on`` at send time.
    The ``agent_model`` / ``agent_thinking_level`` pair is the per-run override (empty
    string = auto). ``use_max`` is the legacy column kept for webhook callers
    (``daiv-max`` label) so the UI can still display the badge; non-webhook surfaces
    pass the override pair instead.
    """
    return Activity.objects.create(
        trigger_type=trigger_type,
        task_result_id=task_result_id,
        repo_id=repo_id,
        ref=ref,
        prompt=prompt,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
        use_max=use_max,
        issue_iid=issue_iid,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention_comment_id,
        scheduled_job=scheduled_job,
        user=user,
        external_username=external_username,
        notify_on=notify_on,
        batch_id=batch_id,
        thread_id=thread_id,
        title=title[: Activity._meta.get_field("title").max_length],
        sandbox_environment=sandbox_environment,
        status=status,
    )


async def acreate_activity(
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
    sandbox_environment: SandboxEnvironment | None = None,
    sandbox_environment_id: str | None = None,
    status: str = ActivityStatus.READY,
) -> Activity:
    """Async variant of create_activity."""
    extra: dict = {}
    if sandbox_environment_id is not None:
        extra["sandbox_environment_id"] = sandbox_environment_id
    else:
        extra["sandbox_environment"] = sandbox_environment
    return await Activity.objects.acreate(
        trigger_type=trigger_type,
        task_result_id=task_result_id,
        repo_id=repo_id,
        ref=ref,
        prompt=prompt,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
        use_max=use_max,
        issue_iid=issue_iid,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention_comment_id,
        scheduled_job=scheduled_job,
        user=user,
        external_username=external_username,
        notify_on=notify_on,
        batch_id=batch_id,
        thread_id=thread_id,
        title=title[: Activity._meta.get_field("title").max_length],
        status=status,
        **extra,
    )


async def _mark_failed_and_release(activity: Activity, *, prefix: str, err: Exception, previous_status: str) -> None:
    """Transition a row to FAILED with finished_at and emit ``activity_finished``.

    Used by the services-layer post-create error paths (enqueue or task-result-id-link
    failure). The emit is best-effort — if it raises, we log loudly and recommend the
    operator run ``release_orphan_queued_threads`` to recover stranded siblings.
    """
    now = timezone.now()
    activity.status = ActivityStatus.FAILED
    activity.error_message = f"{prefix}: {type(err).__name__}: {err}"
    activity.finished_at = now
    if activity.started_at is None:
        activity.started_at = now
    try:
        await activity.asave(update_fields=["status", "error_message", "finished_at", "started_at"])
    except Exception:
        logger.exception("submit_batch_runs: terminal save failed for activity=%s", activity.pk)
    try:
        await asyncio.to_thread(emit_activity_finished_if_terminal, activity, previous_status=previous_status)
    except Exception:
        logger.exception(
            "submit_batch_runs: emit_activity_finished_if_terminal failed for activity=%s; "
            "queued siblings on this thread may be stranded — run release_orphan_queued_threads",
            activity.pk,
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
    """Enqueue N ``run_job_task`` instances sharing a ``batch_id``; record N ``Activity`` rows.

    Each ``RepoTarget`` carries its own ``sandbox_environment_id`` (resolved upstream by
    :func:`sandbox_envs.services.resolve_repo_envs`), so the batch can mix per-repo envs.

    Best-effort: any per-repo exception (enqueue failure or post-enqueue activity-creation
    failure) lands in ``result.failed`` while siblings continue. Callers can use this to
    distinguish "submitted" from "orphaned" and recover accordingly.
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
    if trigger_type == TriggerType.SCHEDULE and scheduled_job is not None:
        schedule_run_base = await Activity.objects.filter(scheduled_job=scheduled_job).acount()

    async def _submit_one(idx: int, target: RepoTarget) -> Activity | BatchSubmitFailure:
        effective_thread_id = thread_id or str(uuid.uuid4())

        activity_title = ""
        if trigger_type == TriggerType.SCHEDULE and scheduled_job is not None:
            activity_title = f"{scheduled_job.name} · run #{schedule_run_base + idx + 1}"

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
            "title": activity_title,
            "sandbox_environment_id": target.sandbox_environment_id,
        }

        # Claim the thread atomically by trying to create a READY row. The partial
        # unique constraint ``activity_one_active_per_thread`` raises IntegrityError
        # when a sibling (READY/RUNNING) is already active on this thread — in that
        # case we fall back to QUEUED, no task enqueue.
        try:
            activity = await acreate_activity(**common_kwargs, task_result_id=None, status=ActivityStatus.READY)
        except IntegrityError:
            try:
                return await acreate_activity(**common_kwargs, task_result_id=None, status=ActivityStatus.QUEUED)
            except Exception as inner_err:
                logger.exception("submit_batch_runs: queued activity creation failed for repo_id=%s", target.repo_id)
                return BatchSubmitFailure(
                    repo_id=target.repo_id,
                    ref=target.ref,
                    error=f"ActivityCreationFailed: {type(inner_err).__name__}: {inner_err}",
                )
        except Exception as err:
            logger.exception("submit_batch_runs: activity creation failed for repo_id=%s", target.repo_id)
            return BatchSubmitFailure(
                repo_id=target.repo_id, ref=target.ref, error=f"ActivityCreationFailed: {type(err).__name__}: {err}"
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
            )
        except Exception as err:  # noqa: BLE001
            logger.exception("submit_batch_runs: enqueue failed for repo_id=%s batch_id=%s", target.repo_id, batch_id)
            await _mark_failed_and_release(
                activity, prefix="enqueue_failed", err=err, previous_status=ActivityStatus.READY
            )
            return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error=f"{type(err).__name__}: {err}")

        try:
            activity.task_result_id = task.id
            await activity.asave(update_fields=["task_result_id"])
        except Exception as save_err:
            # The broker now holds a task this Activity row doesn't link to.
            # Mark the row FAILED so callers see the failure and queued siblings advance;
            # the orphan task itself will execute and ``_sync_activity_for_task`` will no-op
            # (no Activity with that task_result_id).
            logger.exception(
                "submit_batch_runs: failed to link task_result_id=%s to activity=%s (orphan task will run)",
                task.id,
                activity.pk,
            )
            await _mark_failed_and_release(
                activity, prefix="link_failed", err=save_err, previous_status=ActivityStatus.READY
            )
            return BatchSubmitFailure(
                repo_id=target.repo_id, ref=target.ref, error=f"LinkFailed: {type(save_err).__name__}: {save_err}"
            )
        return activity

    # return_exceptions=True guards against BaseException (CancelledError, etc.) aborting the
    # whole batch; _submit_one already catches Exception itself.
    outcomes = await asyncio.gather(*[_submit_one(i, t) for i, t in enumerate(repos)], return_exceptions=True)

    activities: list[Activity] = []
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
            activities.append(outcome)

    if activities and trigger_type in _PROMPT_DRIVEN and prompt:
        try:
            await generate_batch_title_task.aenqueue(batch_id=str(batch_id), prompt=prompt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to enqueue batch title task for batch_id=%s user=%s trigger=%s activities=%d",
                batch_id,
                user.pk if user is not None else None,
                trigger_type,
                len(activities),
            )

    return BatchSubmitResult(batch_id=batch_id, activities=activities, failed=failed)


def submit_batch_runs(**kwargs) -> BatchSubmitResult:
    """Sync wrapper around :func:`asubmit_batch_runs` for cron and sync views."""
    return async_to_sync(asubmit_batch_runs)(**kwargs)
