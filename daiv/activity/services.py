from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from jobs.tasks import run_job_task

from activity.models import Activity

if TYPE_CHECKING:
    from notifications.choices import NotifyOn

    from accounts.models import User
    from schedules.models import ScheduledJob

logger = logging.getLogger("daiv.activity")

MAX_REPOS_PER_BATCH = 20


@dataclass(frozen=True)
class RepoTarget:
    repo_id: str
    ref: str = ""


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
) -> Activity:
    """Async variant of create_activity."""
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
) -> BatchSubmitResult:
    """Enqueue N ``run_job_task`` instances sharing a ``batch_id``; record N ``Activity`` rows.

    Best-effort: any per-repo exception (enqueue failure or post-enqueue activity-creation
    failure) lands in ``result.failed`` while siblings continue. Callers can use this to
    distinguish "submitted" from "orphaned" and recover accordingly.
    """
    _validate(repos)
    batch_id = uuid.uuid4()

    async def _submit_one(target: RepoTarget) -> Activity | BatchSubmitFailure:
        ref_for_task = target.ref or None
        try:
            task = await run_job_task.aenqueue(repo_id=target.repo_id, prompt=prompt, ref=ref_for_task, use_max=use_max)
        except Exception as err:  # noqa: BLE001
            logger.exception("submit_batch_runs: enqueue failed for repo_id=%s batch_id=%s", target.repo_id, batch_id)
            return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error=f"{type(err).__name__}: {err}")

        try:
            return await acreate_activity(
                trigger_type=trigger_type,
                task_result_id=task.id,
                repo_id=target.repo_id,
                ref=target.ref,
                prompt=prompt,
                use_max=use_max,
                scheduled_job=scheduled_job,
                user=user,
                external_username=external_username,
                notify_on=notify_on,
                batch_id=batch_id,
            )
        except Exception:
            logger.exception(
                "submit_batch_runs: activity creation failed for repo_id=%s task_id=%s (orphan job will run)",
                target.repo_id,
                task.id,
            )
            return BatchSubmitFailure(repo_id=target.repo_id, ref=target.ref, error="ActivityCreationFailed")

    # return_exceptions=True guards against BaseException (CancelledError, etc.) aborting the
    # whole batch; _submit_one already catches Exception itself.
    outcomes = await asyncio.gather(*[_submit_one(t) for t in repos], return_exceptions=True)

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

    return BatchSubmitResult(batch_id=batch_id, activities=activities, failed=failed)


def submit_batch_runs(**kwargs) -> BatchSubmitResult:
    """Sync wrapper around :func:`asubmit_batch_runs` for cron and sync views."""
    return async_to_sync(asubmit_batch_runs)(**kwargs)
