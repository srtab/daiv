import logging
import uuid as uuid_mod

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from activity.models import TriggerType
from activity.services import acreate_activity
from django_tasks_db.models import DBTaskResult
from ninja import Router
from ninja.throttling import AuthRateThrottle

from automation.agent.results import parse_agent_result
from chat.api.security import AuthBearer
from core.site_settings import site_settings
from jobs.tasks import run_job_task

from .schemas import JobStatusResponse, JobSubmitRequest, JobSubmitResponse

logger = logging.getLogger("daiv.jobs")

jobs_router = Router(auth=AuthBearer(), tags=["jobs"])


class _LazyThrottle(AuthRateThrottle):
    """Rate throttle that reads the rate from site_settings at startup (avoids import-time DB access)."""

    THROTTLE_RATES = {}

    def get_rate(self):
        return site_settings.jobs_throttle_rate


@jobs_router.post("", response={202: JobSubmitResponse, 503: dict}, throttle=[_LazyThrottle()])
async def submit_job(request: HttpRequest, payload: JobSubmitRequest):
    """
    Submit a job to be processed asynchronously by the DAIV agent.

    Returns a job ID that can be used to poll for the result.
    """
    try:
        result = await run_job_task.aenqueue(
            repo_id=payload.repo_id, prompt=payload.prompt, ref=payload.ref, use_max=payload.use_max
        )
    except Exception:
        logger.exception("Failed to enqueue job for repo_id=%s", payload.repo_id)
        return 503, {"detail": "Failed to submit job. Please try again later."}

    try:
        await acreate_activity(
            trigger_type=TriggerType.API_JOB,
            task_result_id=result.id,
            repo_id=payload.repo_id,
            ref=payload.ref or "",
            prompt=payload.prompt,
            use_max=payload.use_max,
            user=request.auth,
        )
    except Exception:
        logger.exception("Failed to create activity for job %s", result.id)

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

    parsed = parse_agent_result(db_result.return_value)
    return 200, JobStatusResponse(
        job_id=str(db_result.id),
        status=db_result.status,
        result=parsed["response"] or None,
        merge_request_url=parsed["merge_request_web_url"],
        error=error,
        created_at=db_result.enqueued_at,
        started_at=db_result.started_at,
        finished_at=db_result.finished_at,
    )
