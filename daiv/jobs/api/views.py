import logging
import uuid as uuid_mod

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from activity.models import TriggerType
from activity.services import RepoTarget, asubmit_batch_runs
from django_tasks_db.models import DBTaskResult
from ninja import Router

from automation.agent.results import parse_agent_result
from chat.api.security import AuthBearer
from core.api.throttling import JobsRateThrottle
from jobs.tasks import run_job_task

from .schemas import JobStatusResponse, JobSubmitFailureItem, JobSubmitJobItem, JobSubmitRequest, JobSubmitResponse

logger = logging.getLogger("daiv.jobs")

jobs_router = Router(auth=AuthBearer(), tags=["jobs"])


@jobs_router.post("", response={202: JobSubmitResponse, 503: dict}, throttle=[JobsRateThrottle()])
async def submit_job(request: HttpRequest, payload: JobSubmitRequest):
    """Submit a batch of 1-20 agent jobs. Each repository runs as an independent job.

    Returns ``{batch_id, jobs, failed}``. Partial failures at enqueue time are reported
    in ``failed``; the rest of the batch still runs.
    """
    targets = [RepoTarget(repo_id=spec.repo_id, ref=spec.ref or "") for spec in payload.repos]
    result = await asubmit_batch_runs(
        user=request.auth,
        prompt=payload.prompt,
        repos=targets,
        use_max=payload.use_max,
        notify_on=payload.notify_on,
        trigger_type=TriggerType.API_JOB,
    )

    # Pair each non-failed spec (in input order) with the corresponding activity, so the
    # client sees the ref it sent (None vs "") rather than the "" normalized by the service.
    failed_keys = {(f.repo_id, f.ref) for f in result.failed}
    activities_iter = iter(result.activities)
    jobs: list[JobSubmitJobItem] = []
    for spec in payload.repos:
        if (spec.repo_id, spec.ref or "") in failed_keys:
            continue
        activity = next(activities_iter)
        jobs.append(JobSubmitJobItem(job_id=str(activity.task_result_id), repo_id=spec.repo_id, ref=spec.ref))

    failed = [JobSubmitFailureItem(repo_id=f.repo_id, ref=f.ref, error=f.error) for f in result.failed]
    return 202, JobSubmitResponse(batch_id=str(result.batch_id), jobs=jobs, failed=failed)


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
