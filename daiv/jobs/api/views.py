import logging
import uuid as uuid_mod
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django

from ninja import Router
from ninja.errors import HttpError
from sandbox_envs.services import aresolve_repo_envs, resolve_env_for_user
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import RepoTarget, asubmit_batch_runs

from automation.agent.validators import AgentOverrideError, validate_agent_override
from chat.api.security import AuthBearer
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, aassert_can_run
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


@jobs_router.post("", response={202: JobSubmitResponse, 400: dict, 503: dict}, throttle=[JobsRateThrottle()])
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
        agent_model, agent_thinking_level = validate_agent_override(payload.agent_model, payload.agent_thinking_level)
    except AgentOverrideError as err:
        return 400, {"detail": str(err)}

    try:
        await aassert_can_run(request.auth, [spec.repo_id for spec in payload.repos])
    except RepositoryAccessDenied as err:
        # Opaque 404: don't confirm the repo's existence to unauthorized callers.
        raise HttpError(404, REPO_ACCESS_DENIED_MESSAGE) from err

    explicit_env_id = None
    if payload.environment:
        try:
            env = await resolve_env_for_user(request.auth, payload.environment)
        except LookupError as err:
            return 400, {"detail": str(err)}
        explicit_env_id = str(env.id) if env else None

    targets = [RepoTarget(repo_id=spec.repo_id, ref=spec.ref or "") for spec in payload.repos]
    targets = await aresolve_repo_envs(user=request.auth, repos=targets, explicit_env_id=explicit_env_id)
    result = await asubmit_batch_runs(
        user=request.auth,
        prompt=payload.prompt,
        repos=targets,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
        notify_on=payload.notify_on,
        trigger_type=SessionOrigin.API_JOB,
        thread_id=str(payload.thread_id) if payload.thread_id is not None else None,
    )

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
    return 200, JobStatusResponse(
        job_id=str(run.id),
        status=cast("Literal['QUEUED', 'READY', 'RUNNING', 'SUCCESSFUL', 'FAILED']", run.status),
        thread_id=str(run.session_id) if run.session_id else None,
        result=run.result_summary or None,
        merge_request_url=run.merge_request_web_url or None,
        error=error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
