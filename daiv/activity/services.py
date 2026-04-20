from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from jobs.tasks import run_job_task

from activity.models import Activity, TriggerType

if TYPE_CHECKING:
    import uuid

    from accounts.models import User
    from schedules.models import ScheduledJob


def _resolve_notify_on(notify_on: str | None, scheduled_job: ScheduledJob | None) -> str | None:
    if notify_on:
        return notify_on
    if scheduled_job is not None:
        return scheduled_job.notify_on
    return None


def create_activity(
    *,
    trigger_type: str,
    task_result_id: uuid.UUID,
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
    notify_on: str | None = None,
) -> Activity:
    """Create an Activity record linked to a DBTaskResult."""
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
        notify_on=_resolve_notify_on(notify_on, scheduled_job),
    )


async def acreate_activity(
    *,
    trigger_type: str,
    task_result_id: uuid.UUID,
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
    notify_on: str | None = None,
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
        notify_on=_resolve_notify_on(notify_on, scheduled_job),
    )


def submit_ui_run(
    *, user: User, prompt: str, repo_id: str, ref: str = "", use_max: bool = False, notify_on: str | None = None
) -> Activity:
    """Enqueue ``run_job_task`` and record a UI_JOB Activity in a single async boundary crossing.

    ``ref=""`` means "default branch": the task receives ``None`` (its sentinel for default)
    while the Activity row stores the original empty string for display round-tripping.
    """

    async def _submit() -> Activity:
        task = await run_job_task.aenqueue(repo_id=repo_id, prompt=prompt, ref=ref or None, use_max=use_max)
        return await acreate_activity(
            trigger_type=TriggerType.UI_JOB,
            task_result_id=task.id,
            repo_id=repo_id,
            ref=ref,
            prompt=prompt,
            use_max=use_max,
            user=user,
            notify_on=notify_on,
        )

    return async_to_sync(_submit)()
