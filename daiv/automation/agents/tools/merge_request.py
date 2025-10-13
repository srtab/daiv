from __future__ import annotations

import json
import logging

from langchain.tools import tool

from codebase.clients import RepoClient
from codebase.clients.utils import clean_job_logs
from codebase.context import get_repository_ctx

logger = logging.getLogger("daiv.tools")

PIPELINE_TOOL_NAME = "pipeline"
JOB_LOGS_TOOL_NAME = "job_logs"

JOB_LOGS_DEFAULT_LINE_COUNT = 100


@tool(PIPELINE_TOOL_NAME, parse_docstring=True)
def pipeline_tool(placeholder: str = "") -> str:
    """
    Get the latest pipeline/workflow status for a merge/pull request.

    **Usage rules:**
    - Returns JSON formatted pipeline/workflow data with status, ID, SHA, web URL, and categorized jobs;
    - Jobs are separated into failed_jobs, success_jobs, and other_jobs;
    - For failed pipelines/workflows, includes detailed information about failed jobs with failure reasons;
    - Use this tool to understand if a pipeline/workflow failed and which jobs failed;
    - After getting failed job IDs, use the `job_logs` tool to inspect specific job logs;

    Args:
        placeholder: Unused parameter (for compatibility). Leave empty.

    Returns:
        str: JSON formatted pipeline/workflow information including status and job details.
    """
    ctx = get_repository_ctx()

    logger.debug("[%s] Getting pipeline for merge request %d", pipeline_tool.name, ctx.merge_request_id)

    client = RepoClient.create_instance()
    try:
        pipelines = client.get_merge_request_latest_pipelines(ctx.repo_id, ctx.merge_request_id)
    except Exception as e:
        logger.warning(
            "[%s] Failed to get pipeline for merge request %d: %s", pipeline_tool.name, ctx.merge_request_id, e
        )
        return f"error: Failed to get pipeline for merge request {ctx.merge_request_id}. Error: {e}"

    if not pipelines:
        return f"No pipelines found for merge request {ctx.merge_request_id}."

    output_data = []
    for pipeline in pipelines:
        # Separate jobs by status
        failed_jobs = [job for job in pipeline.jobs if job.status == "failed" and not job.allow_failure]
        success_jobs = [job for job in pipeline.jobs if job.status == "success"]
        other_jobs = [job for job in pipeline.jobs if job not in failed_jobs and job not in success_jobs]

        # Build the JSON output
        output_data.append({
            "pipeline_status": pipeline.status,
            "pipeline_id": pipeline.iid or pipeline.id,
            "sha": pipeline.sha,
            "url": pipeline.web_url,
            "total_jobs": len(pipeline.jobs),
            "failed_jobs": [
                {
                    "id": job.id,
                    "name": job.name,
                    "stage": job.stage,
                    "status": job.status,
                    "failure_reason": job.failure_reason,
                }
                for job in failed_jobs
            ],
            "success_jobs": [{"id": job.id, "name": job.name, "stage": job.stage} for job in success_jobs],
            "other_jobs": [
                {"id": job.id, "name": job.name, "stage": job.stage, "status": job.status} for job in other_jobs
            ],
        })

        if pipeline.status in ["failed", "success"]:
            if failed_jobs:
                output_data[-1]["message"] = (
                    "You can use the `job_logs` tool with the Job ID to inspect the logs of failed jobs."
                )
            else:
                output_data[-1]["message"] = "Pipeline completed successfully with no failed jobs."

    return json.dumps(output_data, indent=2)


@tool(JOB_LOGS_TOOL_NAME, parse_docstring=True)
def job_logs_tool(job_id: int, offset_from_end: int = 0, line_count: int = JOB_LOGS_DEFAULT_LINE_COUNT) -> str:
    """
    Get logs from a specific pipeline job with pagination support (bottom-to-top).

    **Usage rules:**
    - Returns paginated log output from a pipeline job, starting from the END (most recent/relevant);
    - For failed jobs, only the output of the failing command is shown (useful for debugging);
    - Use `line_count` to specify the number of lines to read (default: 100);
    - Use `offset_from_end` to paginate backwards through logs (0 = last lines, 100 = skip last 100 lines, etc.);
    - Logs are shown from bottom to top, as errors typically appear at the end.

    Args:
        job_id (int): The job ID to get logs from.
        offset_from_end (int): Number of lines to skip from the end (default: 0 = show last lines).
        line_count (int): The number of lines to read (default: 100).

    Returns:
        str: Formatted job logs with pagination information.
    """
    logger.debug(
        "[%s] Getting logs for job %d (line_count: %d, offset_from_end: %d)",
        job_logs_tool.name,
        job_id,
        line_count,
        offset_from_end,
    )

    ctx = get_repository_ctx()
    client = RepoClient.create_instance()

    # Get job details to determine status
    try:
        job = client.get_job(ctx.repo_id, job_id)
    except Exception as e:
        logger.warning("[%s] Failed to get job details for job %d: %s", job_logs_tool.name, job_id, e)
        return f"error: Failed to get job details for job {job_id}. Error: {e}"

    # Get job logs
    try:
        raw_logs = client.job_log_trace(ctx.repo_id, job_id)
    except Exception as e:
        logger.warning("[%s] Failed to get logs for job %d: %s", job_logs_tool.name, job_id, e)
        return f"error: Failed to get logs for job {job_id}. Error: {e}"

    if not raw_logs:
        return f"No logs found for job {job_id}."

    cleaned_logs = clean_job_logs(raw_logs, client.client_slug, job.is_failed())

    # Split into lines for pagination
    log_lines = cleaned_logs.splitlines()
    total_lines = len(log_lines)

    # Validate offset_from_end
    if offset_from_end < 0:
        offset_from_end = 0
    if offset_from_end >= total_lines:
        return (
            f"error: offset_from_end ({offset_from_end}) exceeds total log lines ({total_lines}). Use a smaller offset."
        )

    # Calculate line range from the end
    # If offset_from_end=0, we want the last line_count lines
    # If offset_from_end=100, we want lines before the last 100
    end_line = total_lines - offset_from_end
    start_line = max(1, end_line - line_count + 1)

    # Extract the requested lines (convert to 0-based indexing)
    selected_lines = log_lines[start_line - 1 : end_line]

    # Build output
    output_lines = [
        f"Job ID: {job_id}",
        f"Job Name: {job.name}",
        f"Job Status: {job.status}",
        f"Job Allow Failure: {job.allow_failure}",
        f"Job Failure Reason: {job.failure_reason}",
        f"Showing lines {start_line}-{end_line} of {total_lines} total lines",
        "",
        "--- Log Output ---",
    ]

    # Add line numbers to the output
    for i, line in enumerate(selected_lines, start=start_line):
        output_lines.append(f"{i}: {line}")

    output_lines.append("--- End of Log Output ---")
    output_lines.append("")

    # Add pagination hints
    if start_line > 1:
        lines_before = start_line - 1
        output_lines.append(
            f"There are {lines_before} earlier lines available. "
            f"Use offset_from_end >= {offset_from_end + line_count} to read more lines."
        )
    else:
        output_lines.append("Start of logs reached.")

    return "\n".join(output_lines)
