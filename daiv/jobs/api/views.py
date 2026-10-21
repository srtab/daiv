import logging
import uuid as uuid_mod
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django

from ninja import Router
from ninja.errors import HttpError
from sessions.admission import AdmissionError, RepoAccessError, aadmit
from sessions.artifacts import aserialize_run_artifacts_for_status
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import RepoTarget, asubmit_batch_runs

from accounts.api.security import AuthBearer
from core.api.throttling import JobsRateThrottle

from .schemas import JobStatusResponse, JobSubmitFailureItem, JobSubmitJobItem, JobSubmitRequest, JobSubmitResponse

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.jobs")

_THREAD_NOT_FOUND = "thread_id not found"

jobs_router = Router(auth=AuthBearer(), tags=["jobs"])


async def _validate_thread_id(thread_id: UUID, user: User) -> tuple[bool, str | None]:
    """Return (ok, error_detail). One opaque message regardless of cause (unknown or not owned).

    Schema-layer validation already constrains ``thread_id`` to a well-formed UUID, so no
    UUID parsing is needed here — a non-existent ID is indistinguishable from a non-owned one.
    """
    owned = await Session.objects.by_owner(user).filter(thread_id=str(thread_id)).aexists()
    if not owned:
        return False, _THREAD_NOT_FOUND
    return True, None


@jobs_router.post("", response={202: JobSubmitResponse, 400: dict, 404: dict, 503: dict}, throttle=[JobsRateThrottle()])
async def submit_job(request: HttpRequest, payload: JobSubmitRequest):
    """Submit a batch of 1-20 agent jobs. Each repository runs as an independent job.

    If ``thread_id`` is supplied, the new job continues an existing thread: exactly one
    repo must be provided and the session owning that thread must belong to the caller.
    If a prior run on the thread is still in flight, the new Run is created in ``QUEUED``
    state and will be released FIFO when the prior run terminates.
    """
    if payload.thread_id is not None:
        if len(payload.repos) != 1:
            return 400, {"detail": "thread_id continuation requires exactly one repo"}
        ok, err = await _validate_thread_id(payload.thread_id, request.auth)
        if not ok:
            return 400, {"detail": err}

    try:
        admission = await aadmit(
            user=request.auth,
            repos=[
                RepoTarget(repo_id=spec.repo_id, ref=spec.ref or "", prompt=spec.prompt or "") for spec in payload.repos
            ],
            agent_model=payload.agent_model,
            agent_thinking_level=payload.agent_thinking_level,
            environment=payload.environment,
        )
        result = await asubmit_batch_runs(
            user=request.auth,
            prompt=payload.prompt,
            repos=admission.targets,
            agent_model=admission.agent_model,
            agent_thinking_level=admission.agent_thinking_level,
            muted=payload.muted,
            trigger_type=SessionOrigin.API_JOB,
            thread_id=str(payload.thread_id) if payload.thread_id is not None else None,
            references=payload.references,
        )
    except RepoAccessError as err:
        # Opaque 404: don't confirm the repo's existence to unauthorized callers.
        raise HttpError(404, str(err)) from err
    except AdmissionError as err:
        return 400, {"detail": str(err)}

    failed_keys = {(f.repo_id, f.ref) for f in result.failed}
    runs_iter = iter(result.runs)
    jobs: list[JobSubmitJobItem] = []
    for spec in payload.repos:
        if (spec.repo_id, spec.ref or "") in failed_keys:
            continue
        run = next(runs_iter)
        jobs.append(
            JobSubmitJobItem(
                job_id=str(run.id),
                repo_id=spec.repo_id,
                ref=spec.ref,
                thread_id=str(run.session_id),
                status=cast("Literal['QUEUED', 'READY']", run.status),
            )
        )

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
        run = await Run.objects.aget(id=run_uuid, user=request.auth)
    except Run.DoesNotExist:
        return 404, {"detail": "Job not found"}

    error = "Job execution failed" if run.status == RunStatus.FAILED else None
    artifacts, artifacts_error = await aserialize_run_artifacts_for_status(run)
    return 200, JobStatusResponse(
        job_id=str(run.id),
        status=cast("Literal['QUEUED', 'READY', 'RUNNING', 'SUCCESSFUL', 'WAITING_INPUT', 'FAILED']", run.status),
        thread_id=str(run.session_id) if run.session_id else None,
        result=run.result_summary or None,
        question=run.question,
        merge_request_url=run.merge_request_web_url or None,
        artifacts=artifacts,
        artifacts_error=artifacts_error,
        error=error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
