import logging
import uuid as uuid_mod

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from django_tasks_db.models import DBTaskResult
from ninja import Router
from ninja.throttling import AuthRateThrottle

from chat.api.security import AuthBearer
from jobs.conf import settings as jobs_settings
from jobs.tasks import run_job_task

from .schemas import JobStatusResponse, JobSubmitRequest, JobSubmitResponse

logger = logging.getLogger("daiv.jobs")

jobs_router = Router(auth=AuthBearer(), tags=["jobs"])


@jobs_router.post(
    "", response={202: JobSubmitResponse, 503: dict}, throttle=[AuthRateThrottle(jobs_settings.THROTTLE_RATE)]
)
async def submit_job(request: HttpRequest, payload: JobSubmitRequest):
    """
    Submit a job to be processed asynchronously by the DAIV agent.

    Returns a job ID that can be used to poll for the result.
    """
    try:
        result = await run_job_task.aenqueue(repo_id=payload.repo_id, prompt=payload.prompt, ref=payload.ref)
    except Exception:
        logger.exception("Failed to enqueue job for repo_id=%s", payload.repo_id)
        return 503, {"detail": "Failed to submit job. Please try again later."}
    return 202, JobSubmitResponse(job_id=result.id)


@jobs_router.get("/{job_id}", response={200: JobStatusResponse, 404: dict})
async def get_job_status(request: HttpRequest, job_id: str):
    """
    Get the status and result of a submitted job.
    """
    try:
        job_uuid = uuid_mod.UUID(job_id)
    except ValueError:
        return 404, {"detail": "Job not found"}

    try:
        db_result = await DBTaskResult.objects.aget(id=job_uuid, task_path=run_job_task.module_path)
    except DBTaskResult.DoesNotExist:
        return 404, {"detail": "Job not found"}

    error = None
    if db_result.status == "FAILED":
        error = "Job execution failed"

    return 200, JobStatusResponse(
        job_id=str(db_result.id),
        status=db_result.status,
        result=db_result.return_value,
        error=error,
        created_at=db_result.enqueued_at,
        started_at=db_result.started_at,
        finished_at=db_result.finished_at,
    )
