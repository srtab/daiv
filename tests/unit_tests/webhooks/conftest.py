import uuid

from django_tasks_db.models import DBTaskResult, get_date_max


async def make_db_task_result() -> uuid.UUID:
    task_id = uuid.uuid4()
    await DBTaskResult.objects.acreate(
        id=task_id,
        status="READY",
        task_path="webhooks.tasks.address_issue_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    return task_id
