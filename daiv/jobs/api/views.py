import logging
import uuid as uuid_mod
from typing import Literal, cast

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django

from ninja import Router
from ninja.errors import HttpError
from sessions.admission import AdmissionError, RepoAccessError
from sessions.artifacts import aserialize_run_artifacts_for_status
from sessions.models import Run, RunStatus, SessionOrigin
from sessions.multi_repo import asubmit_jobs
from sessions.services import BatchSubmitFailure, RepoTarget

from accounts.api.security import AuthBearer
from core.api.throttling import JobsRateThrottle

from .schemas import (
    JobMergeRequestItem,
    JobPublishErrorItem,
    JobStatusResponse,
    JobSubmitFailureItem,
    JobSubmitJobItem,
    JobSubmitRequest,
    JobSubmitResponse,
)

logger = logging.getLogger("daiv.jobs")

jobs_router = Router(auth=AuthBearer(), tags=["jobs"])


@jobs_router.post("", response={202: JobSubmitResponse, 400: dict, 404: dict, 503: dict}, throttle=[JobsRateThrottle()])
async def submit_job(request: HttpRequest, payload: JobSubmitRequest):
    """Submit a batch of 1-20 agent jobs. Each repository runs as an independent job.

    If ``thread_id`` is supplied, the new job continues an existing thread: exactly one
    repo must be provided (a multi-repository session also takes its full list) and the
    session owning that thread must belong to the caller, who has not archived it.
    If a prior run on the thread is still in flight, the new Run is created in ``QUEUED``
    state and will be released FIFO when the prior run terminates.
    """
    try:
        result = await asubmit_jobs(
            user=request.auth,
            repos=[
                RepoTarget(repo_id=spec.repo_id, ref=spec.ref or "", prompt=spec.prompt or "") for spec in payload.repos
            ],
            shared=payload.shared_session,
            thread_id=str(payload.thread_id) if payload.thread_id is not None else None,
            prompt=payload.prompt,
            agent_model=payload.agent_model,
            agent_thinking_level=payload.agent_thinking_level,
            environment=payload.environment,
            muted=payload.muted,
            trigger_type=SessionOrigin.API_JOB,
            references=payload.references,
        )
    except RepoAccessError as err:
        # Opaque 404: don't confirm the repo's existence to unauthorized callers.
        raise HttpError(404, str(err)) from err
    except AdmissionError as err:
        return 400, {"detail": str(err)}

    jobs = [
        JobSubmitJobItem(
            job_id=str(outcome.id),
            repo_id=spec.repo_id,
            ref=spec.ref,
            thread_id=str(outcome.session_id),
            status=cast("Literal['QUEUED', 'READY']", outcome.status),
        )
        # A shared session's single outcome pairs with its main repository, the first spec.
        for spec, outcome in zip(payload.repos, result.outcomes, strict=False)
        if not isinstance(outcome, BatchSubmitFailure)
    ]

    failed = [JobSubmitFailureItem(repo_id=f.repo_id, ref=f.ref, error=f.error) for f in result.failed]
    return 202, JobSubmitResponse(batch_id=str(result.batch_id), jobs=jobs, failed=failed)


@jobs_router.get("/{job_id}", response={200: JobStatusResponse, 404: dict})
async def get_job_status(request: HttpRequest, job_id: str):
    """Get the status and result of a submitted job. Looks up by Run.id."""
    try:
        run_uuid = uuid_mod.UUID(job_id)
    except ValueError:
        return 404, {"detail": "Job not found"}

    try:
        run = await Run.objects.select_related("task_result").aget(id=run_uuid, user=request.auth)
    except Run.DoesNotExist:
        return 404, {"detail": "Job not found"}

    error = "Job execution failed" if run.status == RunStatus.FAILED else None
    artifacts, artifacts_error = await aserialize_run_artifacts_for_status(run)
    return 200, JobStatusResponse(
        job_id=str(run.id),
        status=cast("Literal['QUEUED', 'READY', 'RUNNING', 'SUCCESSFUL', 'WAITING_INPUT', 'FAILED']", run.status),
        thread_id=str(run.session_id) if run.session_id else None,
        result=run.response_text or None,
        question=run.question,
        merge_request_url=run.primary_merge_request_url(),
        merge_requests=[JobMergeRequestItem(**link) for link in run.merge_request_links()],
        publish_errors=[JobPublishErrorItem(**item) for item in run.publish_errors],
        artifacts=artifacts,
        artifacts_error=artifacts_error,
        error=error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
