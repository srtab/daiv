import asyncio
import json
import logging
import uuid as uuid_mod

from django_tasks_db.models import DBTaskResult
from jobs.tasks import run_job_task
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger("daiv.mcp_server")

mcp = FastMCP(
    name="DAIV",
    instructions=(
        "DAIV is an AI-powered development assistant that automates code issues, reviews, and pipeline repairs. "
        "Use the available tools to submit jobs and check their status."
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


TERMINAL_STATUSES = {"SUCCESSFUL", "FAILED"}
POLL_INTERVAL = 2.0
MAX_POLL_DURATION = 600.0  # 10 minutes


@mcp.tool()
async def submit_job(repo_id: str, prompt: str, ref: str | None = None, wait: bool = False) -> str:
    """
    Submit a job to the DAIV agent for a repository.

    The DAIV agent will process the prompt against the specified repository, performing
    code analysis, issue resolution, or other development tasks.

    Args:
        repo_id: The repository identifier (e.g. "owner/repo" or GitLab project path).
        prompt: The instruction or question for the DAIV agent.
        ref: Optional git reference (branch name or commit SHA). Defaults to the repository's default branch.
        wait: If True, wait for the job to complete and return the full result instead of just the job ID.
              The maximum wait time is 10 minutes; if the job hasn't finished by then, the current status is returned.

    Returns:
        A JSON string with either a 'job_id' key for polling status, or an 'error' key if submission failed.
        When wait=True, returns the full job status including result and timing information,
        or the current status if the job hasn't finished within the timeout.
    """
    try:
        result = await run_job_task.aenqueue(repo_id=repo_id, prompt=prompt, ref=ref)
    except Exception:
        logger.exception("Failed to enqueue MCP job for repo_id=%s", repo_id)
        return json.dumps({"error": f"Failed to submit job for repository '{repo_id}'. Please try again later."})

    job_id = str(result.id)
    logger.info("MCP job submitted: job_id=%s, repo_id=%s", job_id, repo_id)

    if not wait:
        return json.dumps({"job_id": job_id})

    return await _poll_job_until_complete(job_id)


def _build_job_response(db_result: DBTaskResult) -> str:
    """Build a JSON response string from a DBTaskResult."""
    error = "Job execution failed." if db_result.status == "FAILED" else None
    return json.dumps({
        "job_id": str(db_result.id),
        "status": db_result.status,
        "result": db_result.return_value,
        "error": error,
        "created_at": db_result.enqueued_at.isoformat() if db_result.enqueued_at else None,
        "started_at": db_result.started_at.isoformat() if db_result.started_at else None,
        "finished_at": db_result.finished_at.isoformat() if db_result.finished_at else None,
    })


async def _poll_job_until_complete(job_id: str) -> str:
    """Poll a job until it reaches a terminal status or the timeout is exceeded."""
    job_uuid = uuid_mod.UUID(job_id)
    elapsed = 0.0
    last_result: DBTaskResult | None = None

    while elapsed < MAX_POLL_DURATION:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        try:
            last_result = await DBTaskResult.objects.aget(id=job_uuid, task_path=run_job_task.module_path)
        except DBTaskResult.DoesNotExist:
            logger.debug("Job %s not yet available, retrying (%.0fs elapsed)", job_id, elapsed)
            continue
        except Exception:
            logger.exception("Failed to poll job status for job_id=%s", job_id)
            return json.dumps({"error": "Failed to retrieve job status. Please try again later."})

        if last_result.status in TERMINAL_STATUSES:
            return _build_job_response(last_result)

    # Timeout — return current status so the caller isn't left without info
    if last_result is not None:
        return _build_job_response(last_result)
    return json.dumps({"error": "Job not found."})


@mcp.tool()
async def get_job_status(job_id: str, wait: bool = False) -> str:
    """
    Get the status and result of a previously submitted job.

    Args:
        job_id: The job ID returned by submit_job.
        wait: If True, wait for the job to complete before returning.
              The maximum wait time is 10 minutes; if the job hasn't finished by then, the current status is returned.

    Returns:
        A JSON string with the job status, result, and timing information (enqueued_at as 'created_at',
        started_at, finished_at). When wait=True, blocks until the job reaches a terminal status or the
        timeout is exceeded, then returns the current status. Returns an error object if the job_id is
        invalid or not found.
    """
    try:
        job_uuid = uuid_mod.UUID(job_id)
    except ValueError:
        return json.dumps({"error": "Invalid job_id format."})

    try:
        db_result = await DBTaskResult.objects.aget(id=job_uuid, task_path=run_job_task.module_path)
    except DBTaskResult.DoesNotExist:
        if wait:
            return await _poll_job_until_complete(job_id)
        return json.dumps({"error": "Job not found."})
    except Exception:
        logger.exception("Failed to retrieve job status for job_id=%s", job_id)
        return json.dumps({"error": "Failed to retrieve job status. Please try again later."})

    if wait and db_result.status not in TERMINAL_STATUSES:
        return await _poll_job_until_complete(job_id)

    return _build_job_response(db_result)
