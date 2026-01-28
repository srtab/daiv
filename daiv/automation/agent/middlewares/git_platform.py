from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import TYPE_CHECKING, Annotated, Literal

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools import ToolRuntime, tool

from codebase.clients.utils import clean_job_logs
from codebase.conf import settings
from codebase.context import RuntimeCtx  # noqa: TC001
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


logger = logging.getLogger("daiv.tools")


GITLAB_MAX_OUTPUT_LINES = 2_000
GITLAB_CLI_TIMEOUT = 30
GITLAB_REQUESTS_TIMEOUT = 15
GITLAB_PER_PAGE = "5"
GITLAB_TOOL_NAME = "gitlab"

GITLAB_TOOL_DESCRIPTION = f"""\
Tool to interact with GitLab API to retrieve information about issues, merge requests, pipelines, jobs, and other resources.

**What this tool does:**
- Retrieves GitLab project resources using the python-gitlab CLI
- Automatically targets the configured project (no `--project-id` needed)
- Returns data in a simplified format by default (`output_mode='simplified'`)
- Paginate the results to the first 5 items
- The output may be truncated bottom-up to {GITLAB_MAX_OUTPUT_LINES} lines by default
- The results are ordered from the most recent to the oldest by default.

**Command Format:**
`<object> <action> <arguments...>`

**Auto-configured:**
- Project ID is automatically set (do NOT pass --project-id)
- `--output` argument is automatically set to according to the `output_mode`, do not pass it manually (it's auto-set).

**Common use cases:**
- Get a specific issue by its IID: `command: project-issue get --iid <issue_iid>, output_mode: 'detailed'`
- Get a specific merge request by its IID: `command: project-merge-request get --iid <merge_request_iid>, output_mode: 'detailed'`
- List pipelines for a merge request: `command: project-merge-request-pipeline list --mr-iid <merge_request_iid>, output_mode: 'simplified'`
- Get the logs/console output of a job: `command: project-job trace --id <job_id>, output_mode: 'detailed'` (the output is truncated to {GITLAB_MAX_OUTPUT_LINES} lines)
- List issues: `command: project-issue list --state opened --labels bug --page <page_number>, output_mode: 'simplified'` (paginate the results to the specified page)

**Bad Examples:**
✗ `gitlab project-issue get --iid 42` (don't include 'gitlab' prefix)
✗ `project-issue get --iid 42 --project-id 123` (project-id auto-added)
✗ `project-issue get --id 42` (use --iid for issues, not --id)
✗ `project-issue list --output json` (don't pass --output manually, it's auto-set)

**Important Notes:**
- Do NOT attempt to fetch URLs from the output - they require authentication
- Always use IID when available, not the internal ID.
- Some actions support additional filters - pass them as additional arguments. If you don't know the available filters, use the `--help` argument to get the list of available arguments.
- When getting a resource by its IID/ID, use `output_mode='detailed'` to get the full output. For listing resources to discover IDs, use the default `output_mode='simplified'`, avoiding long outputs.
- Always quote text that contain spaces or multiline text with double quotes (e.g., project-merge-request-note create --body "This is a note with a space")
"""  # noqa: E501


GIT_PLATFORM_SYSTEM_PROMPT = f"""\
## Git Platform Tools

You have access to the following platform tools:

- `{GITLAB_TOOL_NAME}`: Interact with GitLab API to retrieve issues, merge requests, pipelines, jobs, and other resources. Wraps the `python-gitlab` CLI.

<example>
user: Draft a plan to fix issue #42.
assistant:
  [Call `{GITLAB_TOOL_NAME}("project-issue get --iid 42", output_mode="detailed")`]
assistant:
  [Use the issue title/description/acceptance criteria to draft a fix plan + checklist]
</example>
<example>
user: Fix the failing pipeline for merge request #123.
assistant:
  [Call `{GITLAB_TOOL_NAME}("project-merge-request-pipeline list --mr-iid 123", output_mode="simplified")`]
  [Pick the latest pipeline_id from the list]
  [Call `{GITLAB_TOOL_NAME}("project-pipeline-job list --pipeline-id <pipeline_id>", output_mode="detailed")`]
  [Filter jobs where status is 'failed' and collect the job_ids]
  [For each failing job_id: Call `{GITLAB_TOOL_NAME}("project-job trace --id <job_id>", output_mode="detailed")`]
assistant:
  [Analyze traces → identify root cause → propose changes]
assistant:
  [Implement fixes and describe what to change + where]
</example>

**Notes:**
- Use `output_mode="detailed"` when you need fields like status/name/stage/etc., not just IDs.
- Always fetch job traces for failing jobs before proposing code/config changes."""  # noqa: E501


GITLAB_CLI_DENY_RESOURCES = [
    # Token & credential minting
    "personal-access-token",
    "user-personal-access-token",
    "user-impersonation-token",
    "project-access-token",
    "group-access-token",
    "deploy-token",
    "project-deploy-token",
    "group-deploy-token",
    # Keys (SSH/GPG)  # noqa: ERA001
    "key",
    "user-key",
    "current-user-key",
    "project-key",
    "deploy-key",
    "user-gpg-key",
    "current-user-gpg-key",
    # Webhooks / outbound integrations
    "hook",
    "project-hook",
    "group-hook",
    "project-integration",
    # Secrets & secret-adjacent storage
    "project-variable",
    "group-variable",
    "project-secure-file",
    "project-artifact",
    # Access control / governance / protections
    "project-member",
    "group-member",
    "group-member-all",
    "member-role",
    "group-member-role",
    "project-invitation",
    "group-invitation",
    "project-access-request",
    "group-access-request",
    "project-protected-branch",
    "project-protected-tag",
    "project-protected-environment",
    "project-approval-rule",
    "group-approval-rule",
    "project-merge-request-approval",
    "project-merge-request-approval-rule",
    "project-merge-request-approval-state",
    "project-push-rules",
    "group-push-rules",
    # Import / export / mirroring (bulk movement of code/data)
    "project-export",
    "group-export",
    "project-import",
    "group-import",
    "bulk-import",
    "bulk-import-all-entity",
    "bulk-import-entity",
    "project-pull-mirror",
    "project-remote-mirror",
    # Instance/admin surface (block if there’s any chance the tool token is admin-capable)
    "application",
    "application-settings",
    "application-appearance",
    "application-statistics",
    "license",
    "ldap-group",
    "geo-node",
    "feature",
    "audit-event",
    "group-audit-event",
    "project-audit-event",
]


@tool(GITLAB_TOOL_NAME, description=GITLAB_TOOL_DESCRIPTION)
async def gitlab_tool(
    subcommand: Annotated[
        str,
        "The complete subcommand string in format: '<object> <action> <arguments>'. "
        "Examples: 'project-issue get --iid 42', 'project-merge-request list --state opened'. "
        "Do NOT include 'gitlab' command prefix or --project-id argument - these are auto-added.",
    ],
    runtime: ToolRuntime[RuntimeCtx],
    output_mode: Annotated[
        Literal["detailed", "simplified"],
        "The output format to use (default: 'simplified').",
        "'simplified' is useful for long lists of items to discover IDs. Use it when listing resources.",
        "'detailed' is useful for detailed output. Use it when getting resource details.",
    ] = "simplified",
) -> str:
    """
    Tool to interact with GitLab API using the `python-gitlab` command line interface.

    This tool ensures that the interaction with the GitLab API is done in a more safe and secure way by using
    a subprocess without shell expansion, and that the output is as minimal as possible by paginating the results
    and truncating the output to avoid overwhelming the model with too much data.
    """
    if not subcommand or not subcommand.strip():
        return "error: Subcommand cannot be empty. Format: '<object> <action> <arguments>'"

    try:
        splitted_subcommand = shlex.split(subcommand.strip())
    except ValueError as e:
        return f"error: Failed to parse subcommand: {str(e)}. Check for unmatched quotes."

    if len(splitted_subcommand) < 2:
        return f"error: Incomplete subcommand. Expected format: '<object> <action> <arguments>'. Got: '{subcommand}'"

    resource = splitted_subcommand[0]

    if resource == "gitlab":
        return (
            "error: Do not include 'gitlab' command prefix. "
            "Start directly with the object. Example: 'project-issue get --iid 123'"
        )

    if resource in GITLAB_CLI_DENY_RESOURCES:
        return f"error: The resource '{resource}' is not allowed. Please use a different resource."

    remaining_args = splitted_subcommand[2:]

    if "--project-id" in remaining_args:
        return "error: The project ID is automatically set."

    disallowed_flags = {"--output", "--verbose", "-v", "--fancy"}
    if any(flag in remaining_args for flag in disallowed_flags):
        return "error: The output format is automatically set according to the `output_mode`."

    envs = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108
        "GITLAB_TIMEOUT": str(GITLAB_REQUESTS_TIMEOUT),
        "GITLAB_PRIVATE_TOKEN": settings.GITLAB_AUTH_TOKEN.get_secret_value(),
        "GITLAB_URL": settings.GITLAB_URL.encoded_string(),
        "GITLAB_PER_PAGE": GITLAB_PER_PAGE,
        "GITLAB_USER_AGENT": USER_AGENT,
    }

    args = ["gitlab"]

    if output_mode == "detailed":
        args.append("--verbose")

    args += splitted_subcommand
    args += ["--project-id", runtime.context.repo_id]

    try:
        process = await asyncio.create_subprocess_exec(
            *args, env=envs, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=GITLAB_CLI_TIMEOUT)
    except TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception as e:
            logger.warning("[%s] Failed to kill GitLab process: %s", gitlab_tool.name, e)
        return "error: GitLab command timed out after 30 seconds. The operation may be too complex or the API is slow."
    except Exception as e:
        logger.exception("[%s] Failed to execute GitLab command.", gitlab_tool.name, e)
        return f"error: Failed to execute GitLab command. Details: {str(e)}"

    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8").strip()

        if "404" in stderr_text or "not found" in stderr_text.lower():
            return (
                f"error: Resource not found. "
                f"Please verify the IID/ID exists and you're using the correct argument type. "
                f"Details: {stderr_text}"
            )
        elif "401" in stderr_text or "unauthorized" in stderr_text.lower():
            return "error: Authentication failed. The GitLab token may be invalid or expired."
        elif "403" in stderr_text or "forbidden" in stderr_text.lower():
            return "error: Access denied. You may not have permission to access this resource."

        return f"error: GitLab command failed (exit code {process.returncode}). Details: {stderr_text}"

    output = stdout.decode("utf-8").strip()
    if not output:
        return "Command executed successfully but returned no data"

    if resource == "project-job" and splitted_subcommand[1] == "trace":
        # TODO: evict the output to the file system if it's too long
        cleaned_output = clean_job_logs(output, runtime.context.git_platform)

        return "".join(cleaned_output.splitlines(keepends=True)[-GITLAB_MAX_OUTPUT_LINES:])

    return "".join(output.splitlines(keepends=True)[:GITLAB_MAX_OUTPUT_LINES])


class GitPlatformMiddleware(AgentMiddleware):
    """
    Middleware to add the git platform tools to the agent.

    Example:
        ```python
        from langchain.agents import create_agent
        from langgraph.store.memory import InMemoryStore
        from automation.agent.middlewares.git_platform import GitPlatformMiddleware

        store = InMemoryStore()

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[GitPlatformMiddleware()],
            store=store,
        )
        ```
    """

    def __init__(self) -> None:
        """
        Initialize the middleware.
        """
        self.tools = [gitlab_tool]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git platform system prompt.
        """
        request = request.override(system_prompt=request.system_prompt + "\n\n" + GIT_PLATFORM_SYSTEM_PROMPT)

        return await handler(request)
