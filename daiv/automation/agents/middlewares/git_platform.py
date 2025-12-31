from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Annotated, Literal

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools import ToolRuntime, tool
from langchain_core.messages.content import ContentBlock, create_text_block

from automation.agents.middlewares.multimodal import images_to_content_blocks
from automation.agents.utils import extract_images_from_text
from codebase.clients import RepoClient
from codebase.clients.utils import clean_job_logs
from codebase.context import RuntimeCtx  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


logger = logging.getLogger("daiv.tools")

JOB_LOGS_DEFAULT_LINE_COUNT = 200

GET_ISSUE_TOOL_NAME = "get_issue"
PIPELINE_TOOL_NAME = "pipeline"
JOB_LOGS_TOOL_NAME = "job_logs"

GET_ISSUE_TOOL_DESCRIPTION = """\
Get the issue details by its ID.

**Usage rules:**
- Returns JSON formatted issue data with title, description, state, assignee, author, and labels
- If the issue has images in the description, they are returned as image blocks
- An error message is returned if the issue details cannot be retrieved
"""  # noqa: E501

PIPELINE_TOOL_DESCRIPTION = f"""\
Get the latest pipeline/workflow status for a merge/pull request.

**Usage rules:**
- Returns JSON formatted pipeline/workflow data with status, ID, SHA, web URL, and categorized jobs;
- Jobs are separated into failed_jobs, success_jobs, and other_jobs;
- For failed pipelines/workflows, includes detailed information about failed jobs with failure reasons;
- Use this tool to understand if a pipeline/workflow failed and which jobs failed;
- After getting failed job IDs, use the `{JOB_LOGS_TOOL_NAME}` tool to inspect specific job logs."""  # noqa: E501

JOB_LOGS_TOOL_DESCRIPTION = f"""\
Get logs from a specific pipeline job with pagination support (bottom-to-top).

**Usage rules:**
- Returns paginated log output from a pipeline job, starting from the END (most recent/relevant);
- For failed jobs, only the output of the failing command is shown (useful for debugging);
- Use `line_count` to specify the number of lines to read (default: {JOB_LOGS_DEFAULT_LINE_COUNT});
- Use `offset_from_end` to paginate backwards through logs (0 = last lines, 100 = skip last 100 lines, etc.);
- Logs are shown from bottom to top, as errors typically appear at the end."""  # noqa: E501

GIT_PLATFORM_SYSTEM_PROMPT = f"""\
## Git platform tools

You have access to the following tools to interact with the repository {{repository}} from the {{git_platform}} platform:

- `{GET_ISSUE_TOOL_NAME}`: Get the issue details by its ID.
- `{PIPELINE_TOOL_NAME}`: Get the latest pipeline/workflow status for a merge/pull request.
- `{JOB_LOGS_TOOL_NAME}`: Get logs from a specific pipeline job with pagination support (bottom-to-top)."""  # noqa: E501

ISSUE_GIT_PLATFORM_SYSTEM_PROMPT = f"""\
## Git platform tools

Your are working on the issue #{{issue_id}} created in the repository {{repository}} from the {{git_platform}} platform.
The user will interact with you through the issue comments that will be provided to you as messages.
You should respond to the user's comments with the appropriate actions and tools.

You have access to the following tool to interact with the issue:

- `{GET_ISSUE_TOOL_NAME}`: Get the issue details"""

MERGE_REQUEST_GIT_PLATFORM_SYSTEM_PROMPT = f"""\
## Git platform tools

Your are working on the merge request `{{merge_request_id}}` in the repository {{repository}} from the {{git_platform}} platform.
The user will interact with you through the merge request comments that will be provided to you as messages.
You should respond to the user's comments with the appropriate actions.

You have access to the following tools to interact with the merge request:

- `{PIPELINE_TOOL_NAME}`: Get the latest pipeline/workflow status for the merge request.
- `{JOB_LOGS_TOOL_NAME}`: Get logs from a specific pipeline job for the merge request."""  # noqa: E501


@tool(GET_ISSUE_TOOL_NAME, description=GET_ISSUE_TOOL_DESCRIPTION)
async def get_issue_tool(
    issue_id: Annotated[int, "The issue ID to get details from."], runtime: ToolRuntime[RuntimeCtx]
) -> list[ContentBlock] | str:
    """
    Tool to get the issue details by its ID.
    """
    client = RepoClient.create_instance()

    try:
        issue = client.get_issue(runtime.context.repo_id, issue_id)
    except Exception as e:
        logger.warning("[%s] Failed to get issue details for issue %d: %s", get_issue_tool.name, issue_id, e)
        return f"error: Failed to get issue details for issue {issue_id}. Error: {e}"

    image_blocks = []
    if extracted_images_data := extract_images_from_text(issue.description):
        image_blocks = await images_to_content_blocks(runtime.context.repo_id, extracted_images_data)

    output_data = {
        "id": issue.id,
        "title": issue.title,
        "description": issue.description,
        "state": issue.state,
        "assignee": issue.assignee.username if issue.assignee else None,
        "author": issue.author.username if issue.author else None,
        "labels": issue.labels,
    }

    output_data = [create_text_block(text=json.dumps(output_data, indent=2))]

    if image_blocks:
        output_data.extend(image_blocks)

    return output_data


@tool(PIPELINE_TOOL_NAME, description=PIPELINE_TOOL_DESCRIPTION)
def pipeline_tool(
    merge_request_id: Annotated[int, "The merge request ID to get the latest pipeline/workflow status from."],
    runtime: ToolRuntime[RuntimeCtx],
) -> str:
    """
    Tool to get the latest pipeline/workflow status for a merge/pull request.
    """
    client = RepoClient.create_instance()
    try:
        pipelines = client.get_merge_request_latest_pipelines(runtime.context.repo_id, merge_request_id)
    except Exception as e:
        logger.warning("[%s] Failed to get pipeline for merge request %d: %s", pipeline_tool.name, merge_request_id, e)
        return f"error: Failed to get pipeline for merge request {merge_request_id}. Error: {e}"

    if not pipelines:
        return f"No pipelines found for merge request {merge_request_id}."

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


@tool(JOB_LOGS_TOOL_NAME, description=JOB_LOGS_TOOL_DESCRIPTION)
def job_logs_tool(
    job_id: Annotated[int, "The job ID to get logs from."],
    runtime: ToolRuntime[RuntimeCtx],
    offset_from_end: Annotated[int, "Number of lines to skip from the end (default: 0 = show last lines)."] = 0,
    line_count: Annotated[
        int, f"Number of lines to read (default: {JOB_LOGS_DEFAULT_LINE_COUNT})."
    ] = JOB_LOGS_DEFAULT_LINE_COUNT,
) -> str:
    """
    Tool to get logs from a specific pipeline job with pagination support (bottom-to-top).
    """
    client = RepoClient.create_instance()

    try:
        job = client.get_job(runtime.context.repo_id, job_id)
    except Exception as e:
        logger.warning("[%s] Failed to get job details for job %d: %s", job_logs_tool.name, job_id, e)
        return f"error: Failed to get job details for job {job_id}. Error: {e}"

    try:
        raw_logs = client.job_log_trace(runtime.context.repo_id, job_id)
    except Exception as e:
        logger.warning("[%s] Failed to get logs for job %d: %s", job_logs_tool.name, job_id, e)
        return f"error: Failed to get logs for job {job_id}. Error: {e}"

    if not raw_logs:
        logger.warning("[%s] No logs found for job %d", job_logs_tool.name, job_id)
        return f"No logs found for job {job_id}."

    cleaned_logs = clean_job_logs(raw_logs, client.git_platform, job.is_failed())

    log_lines = cleaned_logs.splitlines()
    total_lines = len(log_lines)

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

    for i, line in enumerate(selected_lines, start=start_line):
        output_lines.append(f"{i}: {line}")

    output_lines.append("--- End of Log Output ---")
    output_lines.append("")

    if start_line > 1:
        lines_before = start_line - 1
        output_lines.append(
            f"There are {lines_before} earlier lines available. "
            f"Use offset_from_end >= {offset_from_end + line_count} to read more lines."
        )
    else:
        output_lines.append("Start of logs reached.")

    return "\n".join(output_lines)


class GitPlatformMiddleware(AgentMiddleware):
    """
    Middleware to add the git platform tools to the agent.
    """

    def __init__(
        self,
        scope: Literal["issue", "merge_request"] | None = None,
        issue_id: int | None = None,
        merge_request_id: int | None = None,
    ) -> None:
        """
        Initialize the middleware.
        """
        self.scope = scope
        self.issue_id = issue_id
        self.merge_request_id = merge_request_id

        if scope == "issue":
            self.tools = [get_issue_tool]
        elif scope == "merge_request":
            self.tools = [pipeline_tool, job_logs_tool]
        else:
            self.tools = [get_issue_tool, pipeline_tool, job_logs_tool]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git platform system prompt.
        """
        system_prompt = GIT_PLATFORM_SYSTEM_PROMPT.format(
            git_platform=request.runtime.context.git_platform.value, repository=request.runtime.context.repo_id
        )

        if self.scope == "issue":
            system_prompt = ISSUE_GIT_PLATFORM_SYSTEM_PROMPT.format(
                git_platform=request.runtime.context.git_platform.value,
                repository=request.runtime.context.repo_id,
                issue_id=self.issue_id,
            )
        elif self.scope == "merge_request":
            system_prompt = MERGE_REQUEST_GIT_PLATFORM_SYSTEM_PROMPT.format(
                git_platform=request.runtime.context.git_platform.value,
                repository=request.runtime.context.repo_id,
                merge_request_id=self.merge_request_id,
            )

        request = request.override(system_prompt=request.system_prompt + "\n\n" + system_prompt)
        return await handler(request)
