from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from jobs.tasks import run_job_task

from activity.models import Activity, ActivityStatus, TriggerType
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
    """
    return Activity.objects.create(
        trigger_type=trigger_type,
        task_result_id=task_result_id,
        repo_id=repo_id,
        ref=ref,
        prompt=prompt,
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


async def asubmit_batch_runs(
    *,
    user: User | None,
    prompt: str,
    repos: list[RepoTarget],
    use_max: bool = False,
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
    if thread_id is not None and len(repos) != 1:
        raise ValueError("thread_id continuation requires exactly one repo")
    batch_id = uuid.uuid4()

    schedule_run_base = 0
    if trigger_type == TriggerType.SCHEDULE and scheduled_job is not None:
        schedule_run_base = await Activity.objects.filter(scheduled_job=scheduled_job).acount()

    async def _submit_one(idx: int, target: RepoTarget) -> Activity | BatchSubmitFailure:
        effective_thread_id = thread_id or str(uuid.uuid4())

        non_terminal_sibling = False
        if thread_id is not None:
            non_terminal_sibling = (
                await Activity.objects
                .filter(thread_id=thread_id)
                .exclude(status__in=list(ActivityStatus.terminal()))
                .aexists()
            )

        ref_for_task = target.ref or None
        task = None
        if not non_terminal_sibling:
            try:
                task = await run_job_task.aenqueue(
                    repo_id=target.repo_id,
                    prompt=prompt,
                    ref=ref_for_task,
                    use_max=use_max,
                    thread_id=effective_thread_id,
                    sandbox_environment_id=target.sandbox_environment_id,
                )
            except Exception as err:  # noqa: BLE001
                logger.exception(
                    "submit_batch_runs: enqueue failed for repo_id=%s batch_id=%s", target.repo_id, batch_id
                )
                return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error=f"{type(err).__name__}: {err}")

        activity_status = ActivityStatus.QUEUED if non_terminal_sibling else ActivityStatus.READY

        activity_title = ""
        if trigger_type == TriggerType.SCHEDULE and scheduled_job is not None:
            activity_title = f"{scheduled_job.name} · run #{schedule_run_base + idx + 1}"

        try:
            activity = await acreate_activity(
                trigger_type=trigger_type,
                task_result_id=task.id if task is not None else None,
                repo_id=target.repo_id,
                ref=target.ref,
                prompt=prompt,
                use_max=use_max,
                scheduled_job=scheduled_job,
                user=user,
                external_username=external_username,
                notify_on=notify_on,
                batch_id=batch_id,
                thread_id=effective_thread_id,
                title=activity_title,
                sandbox_environment_id=target.sandbox_environment_id,
                status=activity_status,
            )
        except Exception:
            logger.exception(
                "submit_batch_runs: activity creation failed for repo_id=%s task_id=%s (orphan job will run)",
                target.repo_id,
                task.id if task is not None else None,
            )
            return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error="ActivityCreationFailed")

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
