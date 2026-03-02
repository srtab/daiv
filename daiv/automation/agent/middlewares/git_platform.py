from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import TYPE_CHECKING, Annotated, Literal, NotRequired

from django.core.cache import cache
from django.utils import timezone

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import OmitFromOutput
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langchain_core.prompts import SystemMessagePromptTemplate
from langgraph.types import Command

from codebase.base import GitPlatform
from codebase.clients.github.utils import get_github_integration
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
Use this tool to inspect the configured GitLab project through the python-gitlab CLI.

This tool is best for retrieving the current state of:
- issues
- merge requests
- pipelines
- jobs
- job traces
- other project-scoped GitLab resources

**Inputs:**
- `subcommand`: a single CLI-like subcommand string in the form `<object> <action> <arguments...>`
- `output_mode`: either `simplified` or `detailed`

**Hard rules:**
- Do NOT include the `gitlab` prefix in `subcommand`
- Do NOT pass `--project-id` (the project is injected automatically)
- Do NOT pass `--output` (it is set automatically from `output_mode`)
- Prefer IID-based lookup when available (for example `--iid` for issues and merge requests)

**Default behavior:**
- The configured project is targeted automatically
- Results are ordered from most recent to oldest by default
- List subcommands return the first 5 items by default unless you paginate with `--page`
- Output may be truncated bottom-up to {DEFAULT_MAX_OUTPUT_LINES} lines

**How to use it well:**
- Use `output_mode="simplified"` to discover the right resource (recent items, IDs, pipeline list)
- Use `output_mode="detailed"` once you know the exact target and need full fields or logs
- Prefer one targeted query over broad listings
- If debugging CI, move in this order: pipeline -> jobs -> failing job trace

**Useful subcommand patterns:**
- Issue by IID: `project-issue get --iid <issue_iid>`
- Merge request by IID: `project-merge-request get --iid <merge_request_iid>`
- MR pipelines: `project-merge-request-pipeline list --mr-iid <merge_request_iid>`
- Pipeline jobs: `project-pipeline-job list --pipeline-id <pipeline_id>`
- Job trace: `project-job trace --id <job_id>`
- Filtered issue list: `project-issue list --state opened --labels bug --page <page_number>`

**Invalid examples:**
- ✗ `gitlab project-issue get --iid 42`
- ✗ `project-issue get --iid 42 --project-id 123`
- ✗ `project-issue get --id 42`
- ✗ `project-issue list --output json`
- ✗ `project list --topic test` (unsupported: this tool is project-scoped)

**Special case: inline merge request comments**
- For a new inline comment on an MR diff, use `project-merge-request-discussion create`, not `project-merge-request-note create`.
- Inline comments require `--position`. Without `--position`, the discussion is created on the MR overview, not on a diff line.
- Before creating an inline comment, first inspect the latest MR diff/version to get the correct SHA triplet (`base_sha`, `start_sha`, `head_sha`).
- For text diff comments, `--position` must include: `position_type="text"`, `base_sha`, `start_sha`, `head_sha`, `old_path`, `new_path`, and the correct line anchor:
  - use `new_line` for an added line
  - use `old_line` for a removed line
  - use both `old_line` and `new_line` for an unchanged line
- To reply to an existing inline thread, use `project-merge-request-discussion-note create --discussion-id <discussion_id>`.
- Never guess an inline position. If the exact diff anchor cannot be determined reliably, prefer a regular MR note instead of posting a misplaced inline comment.

**Special case: review suggestions**
- When leaving an inline MR diff comment that proposes a precise, localized code change, prefer a suggestion block in the comment body instead of plain prose.
- A suggestion is created by putting GitLab suggestion Markdown inside the discussion body (for example, a single-line suggestion uses a `suggestion:-0+0` block).
- Suggestions only work in merge request diff threads. A plain merge request note is not the right place for an applyable suggestion.
- Prefer single-line suggestions by default. Use multi-line suggestions only when the replacement range is clear and tightly scoped.
- Use suggestions only for concrete code replacements. If the feedback is ambiguous, high-level, or not safely expressible as code, use a normal comment instead.

**Operational guidance:**
- Do NOT try to fetch URLs returned by this tool; they require authentication
- If a subcommand fails because you need flags, use targeted help: `<object> <action> --help`
- Always wrap arguments containing spaces or multiline text in double quotes
- When the output is long, extract only the decisive facts needed for the next step"""  # noqa: E501

GITHUB_TOOL_NAME = "gh"

GITHUB_TOOL_DESCRIPTION = f"""\
Use this tool to inspect the configured GitHub repository through the GitHub CLI.

This tool is best for retrieving the current state of:
- issues
- pull requests
- checks
- workflow runs
- job logs
- other repository-scoped GitHub resources

**Inputs:**
- `subcommand`: a single CLI-like subcommand string in the form `<object> <action> [arguments...]>`

**Hard rules:**
- Do NOT include the `gh` prefix in `subcommand`
- Do NOT pass `--repo` or `-R` (the repository is injected automatically)
- Keep listings small and targeted

**Default behavior:**
- The configured repository is targeted automatically
- List subcommands are limited to the first 30 items by default
- Results are ordered from most recent to oldest when supported by the underlying subcommand
- Output may be truncated bottom-up to {DEFAULT_MAX_OUTPUT_LINES} lines

**How to use it well:**
- Start with the smallest subcommand that identifies the exact target
- Prefer direct detail/log commands once you know the relevant issue, PR, run, or job
- If debugging CI, move in this order: PR checks -> failing run/job -> logs
- Prefer one targeted query over broad listings

**Useful subcommand patterns:**
- Issue by number: `issue view <issue_number>`
- Pull request by number: `pr view <pr_number>`
- PR checks: `pr checks <pr_number>`
- Workflow runs: `run list --workflow <workflow_name>`
- Job logs: `run view <run_id> --job <job_id> --log`
- Filtered issue list: `issue list --state open --label bug --limit <n>`

**Invalid examples:**
- ✗ `gh issue view 42`
- ✗ `issue list --repo owner/repo`
- ✗ `issue list --limit 200`

**Operational guidance:**
- Do NOT try to fetch URLs returned by this tool; they require authentication
- Prefer direct log/detail subcommands over relying on summary output
- Always wrap arguments containing spaces or multiline text in double quotes
- When the output is long, extract only the decisive facts needed for the next step"""  # noqa: E501


GIT_PLATFORM_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    f"""\
## Git Platform Tools

Use the available Git platform tool early whenever platform state can change what you should do.

**Core policy:**
- If the user references an issue, PR/MR, pipeline, workflow, job, check, CI failure, review comment, or platform artifact, inspect it before editing code.
- Prefer platform facts over assumptions.
- Do not propose a fix for failing CI until you have inspected the most relevant failing logs/traces available.
- Use the smallest query that identifies the exact resource, then inspect that resource in detail.
- Do not dump raw platform output back to the user; extract only the facts that affect the next action.
- After inspecting platform state, continue with normal coding-agent behavior: inspect the codebase, make the smallest plausible fix, and verify.

**Default debug loop:**
1. Identify the exact target resource
2. Read the most relevant details
3. If CI is failing, inspect the latest failing logs/traces
4. Form a concrete hypothesis
5. Make the smallest likely fix
6. Re-check the relevant platform signal if needed

{{{{#gitlab_platform}}}}
### `{GITLAB_TOOL_NAME}` (GitLab)

Use this tool for GitLab issues, merge requests, pipelines, jobs, and traces.

**GitLab-specific guidance:**
- For issue work, fetch the issue first and use its title/description as the task definition.
- For merge request work, fetch the MR first; if CI is relevant, inspect its latest pipeline before changing code.
- For pipeline failures, do not edit code or CI config until you have read the failing job trace(s).
- Use `output_mode="simplified"` to discover the right resource.
- Use `output_mode="detailed"` to inspect a specific resource or read traces.

**Inline MR comment policy:**
- When the user asks for an inline MR comment, do not create a plain merge request note.
- First identify the exact target line in the current MR diff.
- Then inspect the latest MR diff/version to obtain the SHA triplet needed for anchoring (`base_sha`, `start_sha`, `head_sha`).
- Only then create the comment with `project-merge-request-discussion create --mr-iid <mr_iid> --body "<comment>" --position "<structured position payload>"`.
- If the user is replying to an existing inline thread, use `project-merge-request-discussion-note create --discussion-id <discussion_id>`.
- If the exact inline anchor cannot be resolved safely, fall back to a regular MR note and say the inline position could not be determined reliably.
- Prefer single-line inline comments. Multi-line diff comments are more brittle and should only be attempted when the required range metadata is clearly available.

**Suggestion policy:**
- When leaving an inline review comment and you can express the fix as a small, concrete code replacement, prefer an inline comment with a suggestion block so the author can apply it directly.
- Prefer suggestions for localized edits such as renames, condition fixes, missing guards, small refactors, formatting, or replacing one expression with another.
- Do not use a suggestion when the change is speculative, architectural, spans too much code, depends on broader context, or cannot be anchored precisely to the current diff.
- Prefer a single concise suggestion over a long explanatory comment when the code change itself communicates the fix clearly.
- If using an inline comment, first anchor the discussion correctly to the diff, then place the suggestion block in the comment body.
- Default to single-line suggestions; only use multi-line suggestions when the replacement range is obvious and tightly scoped.

<example>
user: Fix issue #42.
assistant:
  [Call `{GITLAB_TOOL_NAME}("project-issue get --iid 42", output_mode="detailed")`]
assistant:
  [Extract the real problem from the issue]
  [Inspect the relevant code]
  [Make the smallest fix that addresses the issue]
</example>

<example>
user: Fix the failing pipeline for merge request #123.
assistant:
  [Call `{GITLAB_TOOL_NAME}("project-merge-request-pipeline list --mr-iid 123", output_mode="simplified")`]
  [Pick the latest relevant pipeline_id]
  [Call `{GITLAB_TOOL_NAME}("project-pipeline-job list --pipeline-id <pipeline_id>", output_mode="detailed")`]
  [Identify failing jobs]
  [For each failing job: Call `{GITLAB_TOOL_NAME}("project-job trace --id <job_id>", output_mode="detailed")`]
assistant:
  [Use the traces to form a root-cause hypothesis]
  [Then change code or CI config]
</example>

<example>
user: Leave an inline review comment on merge request #123 for src/foo.py line 87.
assistant:
  [Call `gitlab("project-merge-request-diff list --mr-iid 123", output_mode="detailed")`]
  [Select the latest diff/version and extract the SHA fields needed for anchoring]
  [If needed, inspect the diff details to confirm the file path and whether the target line is added, removed, or unchanged]
  [Construct the position payload with `position_type="text"`, `base_sha`, `start_sha`, `head_sha`, `old_path`, `new_path`, and the correct line field(s)]
  [Call `gitlab("project-merge-request-discussion create --mr-iid 123 --body \\"<comment>\\" --position \\"<position payload>\\"", output_mode="detailed")`]
assistant:
  [Confirm the comment was created at the intended diff location]
</example>

<example>
user: Leave an inline review comment on merge request #123 suggesting a safer nil check.
assistant:
  [Identify the exact diff line and create a properly anchored inline MR discussion]
  [Write the comment body as a short explanation plus a GitLab suggestion block containing the replacement code]
assistant:
  [Prefer a single-line suggestion if the fix is localized and directly applicable]
</example>
{{{{/gitlab_platform}}}}

{{{{#github_platform}}}}
### `{GITHUB_TOOL_NAME}` (GitHub)

Use this tool for GitHub issues, pull requests, checks, workflow runs, and logs.

**GitHub-specific guidance:**
- For issue work, fetch the issue first and use its title/body as the task definition.
- For pull request work, fetch the PR first; if CI is relevant, inspect checks and the failing run/job before changing code.
- For workflow failures, do not edit code or workflow config until you have read the most relevant failing logs.
- Prefer direct log/detail subcommands over summaries when possible.

<example>
user: Fix issue #42.
assistant:
  [Call `{GITHUB_TOOL_NAME}("issue view 42")`]
assistant:
  [Extract the real problem from the issue]
  [Inspect the relevant code]
  [Make the smallest fix that addresses the issue]
</example>

<example>
user: Investigate failing workflow/job/check for pull request #123.
assistant:
  [Call `{GITHUB_TOOL_NAME}("pr checks 123")`]
  [Identify the failing check and the relevant run/job identifiers from the returned metadata]
  [Call `{GITHUB_TOOL_NAME}("run view <run_id> --job <job_id> --log")`]
assistant:
  [Use the logs to form a root-cause hypothesis]
  [Then change code or workflow config]
</example>
{{{{/github_platform}}}}""",  # noqa: E501, S608
    "mustache",
)


GITLAB_CLI_ALLOW_COMMANDS: dict[str, set[str] | Literal["*"]] = {
    # Typical issue workflow
    "project-issue": {
        "list",
        "get",
        "create",
        "update",
        "participants",
        "related-merge-requests",
        "time-stats",
        "time-estimate",
        "reset-time-estimate",
        "add-spent-time",
        "reset-spent-time",
        "move",
        "reorder",
        "closed-by",
    },
    "project-issue-note": {"list", "get", "create", "update"},
    "project-issue-discussion": {"list", "get", "create"},
    "project-issue-discussion-note": {"list", "get", "create", "update"},
    "project-issue-award-emoji": {"list", "get", "create", "delete"},
    "project-issue-link": {"list", "create", "delete"},
    "project-label": {"list", "get", "create", "update"},
    # Typical merge request workflow (without merge/approval/admin actions)
    "project-merge-request": {
        "list",
        "get",
        "create",
        "update",
        "time-stats",
        "time-estimate",
        "reset-time-estimate",
        "add-spent-time",
        "reset-spent-time",
        "participants",
        "related-issues",
        "closes-issues",
        "commits",
        "changes",
    },
    "project-merge-request-note": {"list", "get", "create", "update"},
    "project-merge-request-note-award-emoji": {"list", "get", "create", "delete"},
    "project-merge-request-discussion": {"list", "get", "create", "update"},
    "project-merge-request-discussion-note": {"list", "get", "create", "update"},
    "project-merge-request-diff": {"list", "get"},
    "project-merge-request-pipeline": {"list", "create"},
    "project-merge-request-award-emoji": {"list", "get", "create", "delete"},
    "project-merge-request-draft-note": {"list", "get", "create", "update", "delete"},
    "project-merge-request-status-check": {"list"},
    # CI investigation workflow
    "project-pipeline": {"list", "get", "cancel", "retry", "create"},
    "project-pipeline-job": {"list"},
    "project-job": {"list", "get", "artifacts", "artifact", "trace", "retry", "play"},
    # Supporting project data
    "project": {"get", "delete-merged-branches", "languages", "trigger-pipeline"},
    "project-branch": {"list", "get", "create"},
    "project-tag": {"list", "get", "create"},
    "project-release": {"list", "get", "create", "update"},
    "project-release-link": {"list", "get", "create", "update"},
    "project-commit": {"list", "get", "diff", "refs", "merge-requests"},
    "project-environment": {"list", "get"},
    "project-package": {"list", "get"},
    "project-snippet": {"list", "get", "create", "update", "content"},
    "project-snippet-discussion": {"list", "get", "create", "update"},
    "project-snippet-discussion-note": {"list", "get", "create", "update"},
    "project-snippet-note": {"list", "get", "create", "update"},
    "project-snippet-note-award-emoji": {"list", "get", "create", "delete"},
    "project-snippet-award-emoji": {"list", "get", "create", "delete"},
}

GITHUB_CLI_ALLOW_COMMANDS: dict[str, set[str] | Literal["*"]] = {
    # Typical issue workflow
    "issue": {"status", "list", "view", "create", "edit", "comment", "close", "reopen", "lock", "unlock", "develop"},
    # Typical pull request workflow (without merge/destructive operations)
    "pr": {
        "status",
        "list",
        "view",
        "create",
        "edit",
        "comment",
        "review",
        "checks",
        "diff",
        "close",
        "reopen",
        "lock",
        "unlock",
    },
    # CI investigation workflow (read-only)
    "workflow": {"list", "view", "run"},
    "run": {"list", "view", "watch", "download", "rerun"},
    # Supporting read-only project data
    "repo": {"list", "view"},
    "release": {"list", "view", "download", "edit", "upload"},
    "ruleset": {"list", "view", "check"},
    "label": {"list", "create", "edit"},
    "cache": {"list", "delete"},
    "search": {"code", "commits", "issues", "prs"},
}


def _is_allowed_cli_command(
    resource: str, action: str, allow_commands: dict[str, set[str] | Literal["*"]]
) -> tuple[bool, str]:
    allowed_actions = allow_commands.get(resource)
    if allowed_actions is None:
        logger.warning("[git-platform] The subcommand '%s' is not allowed by policy.", resource)
        return False, f"error: The subcommand '{resource}' is not allowed by policy."

    if allowed_actions == "*":
        return True, ""

    if action in allowed_actions:
        return True, ""

    logger.warning("[git-platform] The action '%s' for subcommand '%s' is not allowed by policy.", action, resource)
    return False, f"error: The action '{action}' for subcommand '{resource}' is not allowed by policy."


def _gitlab_has_disallowed_cli_flags(args: list[str]) -> bool:
    for arg in args:
        if arg in {"--output", "--verbose", "-v", "--fancy"}:
            return True
        if arg.startswith("--project-id="):
            return True
        if arg.startswith("-v") and arg != "-v":
            return True
        if arg.startswith("--fancy") and arg != "--fancy":
            return True
    return False


@tool(GITLAB_TOOL_NAME, description=GITLAB_TOOL_DESCRIPTION)
async def gitlab_tool(
    subcommand: Annotated[
        str,
        "Single GitLab CLI subcommand string, parsed with shell-like quoting. "
        "Format: '<object> <action> [arguments...]'. "
        "Do not include the 'gitlab' prefix, '--project-id', or any output/verbosity flags managed by the tool. "
        "Wrap arguments containing spaces in double quotes. "
        "Examples: 'project-issue get --iid 42', 'project-merge-request list --state opened'.",
    ],
    runtime: ToolRuntime[RuntimeCtx],
    output_mode: Annotated[
        Literal["detailed", "simplified"],
        "Controls the detail level of the returned output. "
        "Use 'simplified' for listing/discovery and 'detailed' for inspecting a specific resource or reading logs. "
        "Default: 'simplified'.",
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

    resource, action = splitted_subcommand[0:2]

    if resource == "gitlab":
        return (
            "error: Do not include 'gitlab' command prefix. "
            "Start directly with the object. Example: 'project-issue get --iid 123'"
        )

    is_allowed, policy_message = _is_allowed_cli_command(resource, action, GITLAB_CLI_ALLOW_COMMANDS)
    if not is_allowed:
        return policy_message

    if _gitlab_has_disallowed_cli_flags(splitted_subcommand[2:]):
        return "error: The project ID and output format are automatically set."

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
    args += ["--project-id" if resource != "project" else "--id", runtime.context.repository.slug]

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
        return f"error: GitLab command failed (exit code {process.returncode}). Details: {stderr_text}"

    output = stdout.decode("utf-8").strip()
    if not output:
        return "Command executed successfully but returned no data"

    if resource == "project-job" and splitted_subcommand[1] == "trace":
        # TODO: evict the output to the file system if it's too long
        output = clean_job_logs(output, runtime.context.git_platform)
        return "".join(output.splitlines(keepends=True)[-DEFAULT_MAX_OUTPUT_LINES:])

    return "".join(output.splitlines(keepends=True)[:DEFAULT_MAX_OUTPUT_LINES])


def _get_cached_github_cli_token(runtime: ToolRuntime[RuntimeCtx]) -> tuple[str, dict[str, str | float] | None]:
    """
    Get the cached GitHub CLI token and return state updates if needed.

    The token is cached using a lock to prevent concurrent state updates from parallel tool calls.

    Returns:
        A tuple of (token, state_updates). state_updates is None if no update is needed.
    """
    assert settings.GITHUB_INSTALLATION_ID is not None, "GITHUB_INSTALLATION_ID is not set"

    token = runtime.state.get("github_token")
    expires_at = runtime.state.get("github_token_expires_at")

    if not token or expires_at is None or timezone.now().timestamp() > expires_at:
        thread_id = runtime.config.get("configurable", {}).get("thread_id", runtime.context.repository.slug)

        with cache.lock(f"github_lock_{thread_id}", blocking=True):
            cache_key = f"github_token_{thread_id}"

            if data := cache.get(cache_key):
                # this means a parallel tool call already acquired the lock and cached the token
                return data["github_token"], None

            # no token in cache, so we need to get a new one and cache it
            access_token = get_github_integration().get_access_token(settings.GITHUB_INSTALLATION_ID)

            ttl = access_token.expires_at.timestamp() - timezone.now().timestamp()
            data = {"github_token": access_token.token, "github_token_expires_at": access_token.expires_at.timestamp()}
            cache.set(cache_key, data, timeout=ttl)
            return access_token.token, data

    return token, None


def _gh_has_disallowed_cli_flags(args: list[str]) -> bool:
    for arg in args:
        if arg in {"--repo", "-R", "--hostname"}:
            return True
        if arg.startswith("--repo=") or arg.startswith("--hostname="):
            return True
        if arg.startswith("-R") and arg != "-R":
            return True
    return False


@tool(GITHUB_TOOL_NAME, description=GITHUB_TOOL_DESCRIPTION)
async def github_tool(
    subcommand: Annotated[
        str,
        "Single GitHub CLI subcommand string, parsed with shell-like quoting. "
        "Format: '<object> <action> [arguments...]'. "
        "Do not include the 'gh' prefix. "
        "Do not pass '--repo', '-R', or '--hostname' (these are managed or restricted by the tool). "
        "Wrap arguments containing spaces in double quotes. "
        "Examples: 'issue view 42', 'pr list --state open'.",
    ],
    runtime: ToolRuntime[RuntimeCtx],
) -> str | Command:
    """
    Tool to interact with GitHub API using the `gh` command line interface.

    This tool ensures that the interaction with the GitHub API is done in a more safe and secure way by using
    a subprocess without shell expansion, and that the output is as minimal as possible by paginating the results
    and truncating the output to avoid overwhelming the model with too much data.
    """
    if not subcommand or not subcommand.strip():
        return "error: Subcommand cannot be empty. Format: '<object> <action> [arguments...]'"

    try:
        splitted_subcommand = shlex.split(subcommand.strip())
    except ValueError as e:
        return f"error: Failed to parse subcommand: {str(e)}. Check for unmatched quotes."

    if len(splitted_subcommand) < 2:
        return f"error: Incomplete subcommand. Expected format: '<object> <action> [arguments...]'. Got: '{subcommand}'"

    resource, action = splitted_subcommand[0:2]

    if resource == "gh":
        return "error: Do not include 'gh' command prefix. Start directly with the object. Example: 'issue view 123'"

    if not action:
        return f"error: Incomplete subcommand. Expected format: '<object> <action> [arguments...]'. Got: '{subcommand}'"

    is_allowed, policy_message = _is_allowed_cli_command(resource, action, GITHUB_CLI_ALLOW_COMMANDS)
    if not is_allowed:
        return policy_message

    if _gh_has_disallowed_cli_flags(splitted_subcommand[2:]):
        return "error: The repository and hostname are automatically set. Do not pass --repo, -R, or --hostname."

    token, state_update = _get_cached_github_cli_token(runtime)

    envs = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
        "GH_TOKEN": token,
        "GH_PAGER": "cat",
    }

    args = ["gh"]
    args += splitted_subcommand
    if resource != "api":
        args += ["--repo", runtime.context.repository.slug]

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
        return f"error: GitHub command failed (exit code {process.returncode}). Details: {stderr_text}"

    output = stdout.decode("utf-8").strip()
    if not output:
        output = "Command executed successfully but returned no data"
    elif resource == "run" and action == "view" and "--log" in splitted_subcommand:
        # TODO: evict the output to the file system if it's too long
        output = clean_job_logs(output, runtime.context.git_platform)
        output = "".join(output.splitlines(keepends=True)[-DEFAULT_MAX_OUTPUT_LINES:])
    else:
        output = "".join(output.splitlines(keepends=True)[:DEFAULT_MAX_OUTPUT_LINES])

    # Return Command with state update if token was cached/refreshed
    if state_update:
        tool_message = ToolMessage(content=output, tool_call_id=runtime.tool_call_id)
        state_update["messages"] = [tool_message]
        return Command(update=state_update)

    return output


class GitPlatformState(AgentState):
    github_token: NotRequired[Annotated[str | None, OmitFromOutput]]
    github_token_expires_at: NotRequired[Annotated[float | None, OmitFromOutput]]


class GitPlatformMiddleware(AgentMiddleware):
    state_schema = GitPlatformState

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
