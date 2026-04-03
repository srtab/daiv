import json
import logging
import uuid as uuid_mod

from django_tasks_db.models import DBTaskResult
from jobs.tasks import run_job_task
from mcp.server.fastmcp import FastMCP

from codebase.clients import RepoClient

logger = logging.getLogger("daiv.mcp_server")

mcp = FastMCP(
    name="DAIV",
    instructions=(
        "DAIV is an AI-powered development assistant that automates code issues, reviews, and pipeline repairs. "
        "Use the available tools to submit jobs, check their status, and discover repositories."
    ),
    stateless_http=True,
)


@mcp.tool()
async def submit_job(repo_id: str, prompt: str, ref: str | None = None) -> str:
    """
    Submit a job to the DAIV agent for a repository.

    The DAIV agent will process the prompt against the specified repository, performing
    code analysis, issue resolution, or other development tasks.

    Args:
        repo_id: The repository identifier (e.g. "owner/repo" or GitLab project path).
        prompt: The instruction or question for the DAIV agent.
        ref: Optional git reference (branch name or commit SHA). Defaults to the repository's default branch.

    Returns:
        A JSON string with the job_id for polling status.
    """
    try:
        result = await run_job_task.aenqueue(repo_id=repo_id, prompt=prompt, ref=ref)
    except Exception:
        logger.exception("Failed to enqueue MCP job for repo_id=%s", repo_id)
        return json.dumps({"error": "Failed to submit job. Please try again later."})

    logger.info("MCP job submitted: job_id=%s, repo_id=%s", result.id, repo_id)
    return json.dumps({"job_id": str(result.id)})


@mcp.tool()
async def get_job_status(job_id: str) -> str:
    """
    Get the status and result of a previously submitted job.

    Args:
        job_id: The job ID returned by submit_job.

    Returns:
        A JSON string with the job status, result, and timing information.
    """
    try:
        job_uuid = uuid_mod.UUID(job_id)
    except ValueError:
        return json.dumps({"error": "Invalid job_id format."})

    try:
        db_result = await DBTaskResult.objects.aget(id=job_uuid, task_path=run_job_task.module_path)
    except DBTaskResult.DoesNotExist:
        return json.dumps({"error": "Job not found."})

    error = None
    if db_result.status == "FAILED":
        error = "Job execution failed."

    return json.dumps({
        "job_id": str(db_result.id),
        "status": db_result.status,
        "result": db_result.return_value,
        "error": error,
        "created_at": db_result.enqueued_at.isoformat() if db_result.enqueued_at else None,
        "started_at": db_result.started_at.isoformat() if db_result.started_at else None,
        "finished_at": db_result.finished_at.isoformat() if db_result.finished_at else None,
    })


@mcp.tool()
def list_repositories(search: str | None = None, topics: list[str] | None = None) -> str:
    """
    List repositories that DAIV has access to.

    Use this to discover available repositories before submitting a job.

    Args:
        search: Optional search query to filter repositories by name.
        topics: Optional list of topics to filter repositories.

    Returns:
        A JSON string with the list of repositories including their IDs, names, and default branches.
    """
    try:
        client = RepoClient.create_instance()
        repos = client.list_repositories(search=search, topics=topics)
    except NotImplementedError:
        # GitHub client does not support search
        client = RepoClient.create_instance()
        repos = client.list_repositories(topics=topics)
    except Exception:
        logger.exception("Failed to list repositories")
        return json.dumps({"error": "Failed to list repositories."})

    return json.dumps({
        "repositories": [
            {
                "repo_id": repo.slug,
                "name": repo.name,
                "default_branch": repo.default_branch,
                "url": repo.html_url,
                "topics": repo.topics,
            }
            for repo in repos
        ]
    })
