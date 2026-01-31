from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import TYPE_CHECKING, Annotated, Literal

from github import Auth, Consts, GithubIntegration
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools import ToolRuntime, tool
from langchain_core.prompts import SystemMessagePromptTemplate

from codebase.base import GitPlatform
from codebase.clients.utils import clean_job_logs
from codebase.conf import settings
from codebase.context import RuntimeCtx  # noqa: TC001
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


logger = logging.getLogger("daiv.tools")


DEFAULT_MAX_OUTPUT_LINES = 2_000
DEFAULT_CLI_TIMEOUT = 30

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
- The output may be truncated bottom-up to {DEFAULT_MAX_OUTPUT_LINES} lines by default
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
- Get the logs/console output of a job: `command: project-job trace --id <job_id>, output_mode: 'detailed'` (the output is truncated to {DEFAULT_MAX_OUTPUT_LINES} lines)
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

GITHUB_TOOL_NAME = "gh"

GITHUB_TOOL_DESCRIPTION = f"""\
Tool to interact with GitHub API to retrieve information about issues, pull requests, workflows, runs, and other resources.

**What this tool does:**
- Retrieves GitHub repository resources using the GitHub CLI
- Automatically targets the configured repository (no `--repo` needed)
- List commands are limited to the first 30 items by default
- The output may be truncated bottom-up to {DEFAULT_MAX_OUTPUT_LINES} lines by default
- The results are ordered from the most recent to the oldest by default when supported

**Command Format:**
`<object> <action> <arguments...>`

**Auto-configured:**
- Repository is automatically set (do NOT pass `--repo` or `-R`)

**Common use cases:**
- Get a specific issue by its number: `command: issue view <issue_number>, output_mode: 'detailed'`
- Get a specific pull request by its number: `command: pr view <pr_number>, output_mode: 'detailed'`
- List workflow runs: `command: run list --workflow <workflow_name>, output_mode: 'simplified'`
- List issues: `command: issue list --state open --label bug --limit <n>, output_mode: 'simplified'`

**Bad Examples:**
✗ `gh issue view 42` (don't include 'gh' prefix)
✗ `issue list --repo owner/repo` (repo auto-added)
✗ `issue list --limit 200` (avoid huge outputs; keep limits small)

**Important Notes:**
- Do NOT attempt to fetch URLs from the output - they require authentication
- Always quote text that contains spaces or multiline text with double quotes
- For listing resources to discover IDs, use the default `output_mode='simplified'`
"""  # noqa: E501


GIT_PLATFORM_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    f"""\
## Git Platform Tools

You have access to the following tools to interact with the Git platform:

{{{{#gitlab_platform}}}}
- `{GITLAB_TOOL_NAME}`: Interact with GitLab API to retrieve issues, merge requests, pipelines, jobs, and other resources. Wraps the `python-gitlab` CLI.

<example>
user: Draft a plan to fix issue #42.
assistant:
  [Call `{GITLAB_TOOL_NAME}("project-issue get --iid 42", output_mode="detailed")`]
assistant:
  [Use the issue title/description to draft a fix plan + checklist]
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
  [Analyze traces → identify root cause → Implement fixes]
</example>

**Notes:**
- Use `output_mode="detailed"` when you need fields like status/name/stage/etc., not just IDs.
- Always fetch job traces for failing jobs before proposing code/config changes.
{{{{/gitlab_platform}}}}
{{{{#github_platform}}}}
- `{GITHUB_TOOL_NAME}`: Interact with GitHub API to retrieve issues, pull requests, workflows, runs, and other resources. Wraps the `gh` CLI.
<example>
user: Draft a plan to fix issue #42.
assistant:
  [Call `{GITHUB_TOOL_NAME}("issue view 42 --json title,body,labels")`]
  [Use the issue title/description to draft a fix plan + checklist]
</example>
<example>
user: Investigate failing workflow/job/check for pull request #123.
assistant:
  [Call `{GITHUB_TOOL_NAME}("pr checks 123")`]
  [Pick the failing check run_id and job_id from the url, if any]
  [Call `{GITHUB_TOOL_NAME}("run view <run_id> --job <job_id> --log")`]
assistant:
  [Analyze logs → identify root cause → Implement fixes]
</example>
{{{{/github_platform}}}}""",  # noqa: E501
    "mustache",
)


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

GITHUB_CLI_DENY_COMMANDS = {
    # Token & credential minting
    "auth",
    "secret",
    "variable",
    # Keys (SSH/GPG)  # noqa: ERA001
    "ssh-key",
    "gpg-key",
    # Extensions / config / local settings
    "alias",
    "config",
    "extension",
    # Codespaces and other remote access surfaces
    "codespace",
    # Copilot access
    "copilot",
}

GITHUB_CLI_DENY_ACTIONS = {
    "repo": {"create", "delete", "rename", "fork", "archive", "unarchive", "edit", "set-default-branch", "sync"}
}

GITHUB_API_DENY_PATH_SEGMENTS = {"/hooks", "/keys", "/secrets", "/deploy_keys"}

GITHUB_API_DISALLOWED_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}

GITHUB_CLI_DISALLOWED_FLAGS = {"--repo", "-R", "--hostname"}


def _get_github_cli_token() -> str | None:
    base_url = Consts.DEFAULT_BASE_URL
    if settings.GITHUB_URL:
        base_url = str(settings.GITHUB_URL)

    try:
        integration = GithubIntegration(
            auth=Auth.AppAuth(settings.GITHUB_APP_ID, settings.GITHUB_PRIVATE_KEY.get_secret_value()),
            base_url=base_url,
            user_agent=USER_AGENT,
        )
        return integration.get_access_token(settings.GITHUB_INSTALLATION_ID).token
    except Exception:
        logger.exception("[%s] Failed to generate GitHub token.", GITHUB_TOOL_NAME)
        return None


def _get_subcommand_action(args: list[str]) -> str | None:
    for arg in args[1:]:
        if arg.startswith("-"):
            continue
        return arg
    return None


def _has_disallowed_cli_flags(args: list[str]) -> bool:
    for arg in args:
        if arg in GITHUB_CLI_DISALLOWED_FLAGS:
            return True
        if arg.startswith("--repo=") or arg.startswith("--hostname="):
            return True
        if arg.startswith("-R") and arg != "-R":
            return True
    return False


def _validate_github_api_args(args: list[str]) -> str | None:
    for arg in args:
        if arg in GITHUB_API_DISALLOWED_FLAGS:
            return (
                "error: gh api only supports read-only GET requests. "
                "Remove write flags like --method, -X, --field, or --input."
            )
        if arg.startswith("--method=") or arg.startswith("-X"):
            return (
                "error: gh api only supports read-only GET requests. "
                "Remove write flags like --method, -X, --field, or --input."
            )
        if arg.startswith("--field=") or arg.startswith("--raw-field=") or arg.startswith("--input="):
            return (
                "error: gh api only supports read-only GET requests. "
                "Remove write flags like --method, -X, --field, or --input."
            )

    endpoint = _get_subcommand_action(args)
    if not endpoint:
        return "error: gh api requires an endpoint, e.g. 'api repos/<owner>/<repo>/issues'."

    normalized_endpoint = endpoint.lower()
    if any(segment in normalized_endpoint for segment in GITHUB_API_DENY_PATH_SEGMENTS):
        return "error: Access to hooks, keys, or secrets endpoints is not allowed."

    return None


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

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DEFAULT_CLI_TIMEOUT)
    except TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception as e:
            logger.warning("[%s] Failed to kill GitLab process: %s", gitlab_tool.name, e)
        return "error: GitLab command timed out after 30 seconds. The operation may be too complex or the API is slow."
    except Exception as e:
        logger.exception("[%s] Failed to execute GitLab command.", gitlab_tool.name)
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

        return "".join(cleaned_output.splitlines(keepends=True)[-DEFAULT_MAX_OUTPUT_LINES:])

    return "".join(output.splitlines(keepends=True)[:DEFAULT_MAX_OUTPUT_LINES])


@tool(GITHUB_TOOL_NAME, description=GITHUB_TOOL_DESCRIPTION)
async def github_tool(
    subcommand: Annotated[
        str,
        "The complete subcommand string in format: '<object> <action> <arguments>'. "
        "Examples: 'issue view 42', 'pr list --state open'. "
        "Do NOT include 'gh' command prefix or --repo argument - these are auto-added.",
    ],
    runtime: ToolRuntime[RuntimeCtx],
) -> str:
    """
    Tool to interact with GitHub API using the `gh` command line interface.

    This tool ensures that the interaction with the GitHub API is done in a more safe and secure way by using
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
    action = _get_subcommand_action(splitted_subcommand)

    if resource == "gh":
        return "error: Do not include 'gh' command prefix. Start directly with the object. Example: 'issue view 123'"

    if not action:
        return f"error: Incomplete subcommand. Expected format: '<object> <action> <arguments>'. Got: '{subcommand}'"

    if resource in GITHUB_CLI_DENY_COMMANDS:
        return f"error: The command '{resource}' is not allowed. Please use a different command."

    if resource in GITHUB_CLI_DENY_ACTIONS and action in GITHUB_CLI_DENY_ACTIONS[resource]:
        return f"error: The action '{action}' for command '{resource}' is not allowed."

    if _has_disallowed_cli_flags(splitted_subcommand):
        return "error: The repository and host are automatically set. Do not pass --repo, -R, or --hostname."

    if resource == "api":
        api_error = _validate_github_api_args(splitted_subcommand)
        if api_error:
            return api_error

    envs = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108
        "GH_TOKEN": _get_github_cli_token(),
        "GH_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
    }

    args = ["gh"]
    args += splitted_subcommand
    args += ["--repo", runtime.context.repo_id]

    try:
        process = await asyncio.create_subprocess_exec(
            *args, env=envs, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DEFAULT_CLI_TIMEOUT)
    except TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception as e:
            logger.warning("[%s] Failed to kill GitHub process: %s", github_tool.name, e)
        return "error: GitHub command timed out after 30 seconds. The operation may be too complex or the API is slow."
    except Exception as e:
        logger.exception("[%s] Failed to execute GitHub command.", github_tool.name)
        return f"error: Failed to execute GitHub command. Details: {str(e)}"

    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8").strip()
        stderr_lower = stderr_text.lower()

        if "404" in stderr_lower or "not found" in stderr_lower:
            return (
                "error: Resource not found. "
                "Please verify the number/ID exists and you're using the correct argument type. "
                f"Details: {stderr_text}"
            )
        if "401" in stderr_lower or "unauthorized" in stderr_lower:
            return "error: Authentication failed. The GitHub token may be invalid or expired."
        if "403" in stderr_lower or "forbidden" in stderr_lower:
            return "error: Access denied. You may not have permission to access this resource."

        return f"error: GitHub command failed (exit code {process.returncode}). Details: {stderr_text}"

    output = stdout.decode("utf-8").strip()
    if not output:
        return "Command executed successfully but returned no data"

    return "".join(output.splitlines(keepends=True)[:DEFAULT_MAX_OUTPUT_LINES])


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

    def __init__(self, git_platform: GitPlatform) -> None:
        """
        Initialize the middleware.
        """
        super().__init__()

        self.tools = []

        if git_platform == GitPlatform.GITLAB:
            self.tools.append(gitlab_tool)
        elif git_platform == GitPlatform.GITHUB:
            self.tools.append(github_tool)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git platform system prompt.
        """
        if request.runtime.context.git_platform in {GitPlatform.GITLAB, GitPlatform.GITHUB}:
            git_platform_system_prompt = GIT_PLATFORM_SYSTEM_PROMPT.format(
                gitlab_platform=request.runtime.context.git_platform == GitPlatform.GITLAB,
                github_platform=request.runtime.context.git_platform == GitPlatform.GITHUB,
            )
            request = request.override(
                system_prompt=request.system_prompt + "\n\n" + git_platform_system_prompt.content
            )

        return await handler(request)
