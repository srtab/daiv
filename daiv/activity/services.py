from __future__ import annotations

from typing import TYPE_CHECKING

from activity.models import Activity

if TYPE_CHECKING:
    import uuid

    from accounts.models import User
    from schedules.models import ScheduledJob


def create_activity(
    *,
    trigger_type: str,
    task_result_id: uuid.UUID,
    repo_id: str,
    ref: str = "",
    prompt: str = "",
    issue_iid: int | None = None,
    merge_request_iid: int | None = None,
    mention_comment_id: str = "",
    scheduled_job: ScheduledJob | None = None,
    user: User | None = None,
    external_username: str = "",
) -> Activity:
    """Create an Activity record linked to a DBTaskResult."""
    return Activity.objects.create(
        trigger_type=trigger_type,
        task_result_id=task_result_id,
        repo_id=repo_id,
        ref=ref,
        prompt=prompt,
        issue_iid=issue_iid,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention_comment_id,
        scheduled_job=scheduled_job,
        user=user,
        external_username=external_username,
    )


async def acreate_activity(
    *,
    trigger_type: str,
    task_result_id: uuid.UUID,
    repo_id: str,
    ref: str = "",
    prompt: str = "",
    issue_iid: int | None = None,
    merge_request_iid: int | None = None,
    mention_comment_id: str = "",
    scheduled_job: ScheduledJob | None = None,
    user: User | None = None,
    external_username: str = "",
) -> Activity:
    """Async variant of create_activity."""
    return await Activity.objects.acreate(
        trigger_type=trigger_type,
        task_result_id=task_result_id,
        repo_id=repo_id,
        ref=ref,
        prompt=prompt,
        issue_iid=issue_iid,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention_comment_id,
        scheduled_job=scheduled_job,
        user=user,
        external_username=external_username,
    )
