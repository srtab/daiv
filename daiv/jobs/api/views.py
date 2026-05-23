import logging
import uuid as uuid_mod
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django

from activity.models import Activity, ActivityStatus, TriggerType
from activity.services import RepoTarget, asubmit_batch_runs
from ninja import Router
from sandbox_envs.services import aresolve_repo_envs, resolve_env_for_user

from automation.agent.validators import AgentOverrideError, validate_agent_override
from chat.api.security import AuthBearer
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
    latest = await Activity.objects.filter(thread_id=str(thread_id)).order_by("-created_at").afirst()
    if latest is None or latest.user_id != user.pk:
        return False, _THREAD_NOT_FOUND
    return True, None


@jobs_router.post("", response={202: JobSubmitResponse, 400: dict, 503: dict}, throttle=[JobsRateThrottle()])
async def submit_job(request: HttpRequest, payload: JobSubmitRequest):
    """Submit a batch of 1-20 agent jobs. Each repository runs as an independent job.

    If ``thread_id`` is supplied, the new job continues an existing thread: exactly one
    repo must be provided and the most recent Activity on that thread must belong to the
    caller. If a prior run on the thread is still in flight, the new Activity is created
    in ``QUEUED`` state and will be released FIFO when the prior run terminates.
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
        trigger_type=TriggerType.API_JOB,
        thread_id=str(payload.thread_id) if payload.thread_id is not None else None,
    )

    failed_keys = {(f.repo_id, f.ref) for f in result.failed}
    activities_iter = iter(result.activities)
    jobs: list[JobSubmitJobItem] = []
    for spec in payload.repos:
        if (spec.repo_id, spec.ref or "") in failed_keys:
            continue
        activity = next(activities_iter)
        jobs.append(
            JobSubmitJobItem(
                job_id=str(activity.id),
                repo_id=spec.repo_id,
                ref=spec.ref,
                thread_id=str(activity.thread_id),
                status=cast("Literal['QUEUED', 'READY']", activity.status),
            )
        )

    failed = [JobSubmitFailureItem(repo_id=f.repo_id, ref=f.ref, error=f.error) for f in result.failed]
    return 202, JobSubmitResponse(batch_id=str(result.batch_id), jobs=jobs, failed=failed)


@jobs_router.get("/{job_id}", response={200: JobStatusResponse, 404: dict})
async def get_job_status(request: HttpRequest, job_id: str):
    """Get the status and result of a submitted job. Looks up by Activity.id."""
    try:
        activity_uuid = uuid_mod.UUID(job_id)
    except ValueError:
        return 404, {"detail": "Job not found"}

    try:
        activity = await Activity.objects.aget(id=activity_uuid, user=request.auth)
    except Activity.DoesNotExist:
        return 404, {"detail": "Job not found"}

    error = "Job execution failed" if activity.status == ActivityStatus.FAILED else None
    return 200, JobStatusResponse(
        job_id=str(activity.id),
        status=cast("Literal['QUEUED', 'READY', 'RUNNING', 'SUCCESSFUL', 'FAILED']", activity.status),
        thread_id=str(activity.thread_id) if activity.thread_id else None,
        result=activity.result_summary or None,
        merge_request_url=activity.merge_request_web_url or None,
        error=error,
        created_at=activity.created_at,
        started_at=activity.started_at,
        finished_at=activity.finished_at,
    )
