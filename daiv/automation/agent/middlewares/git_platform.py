from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from typing import TYPE_CHECKING, Annotated, Literal, NotRequired

from django.core.cache import cache
from django.utils import timezone

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.utils import sanitize_tool_call_id
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import OmitFromOutput
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langchain_core.prompts import SystemMessagePromptTemplate
from langgraph.types import Command

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.clients.github.utils import get_github_integration
from codebase.clients.utils import clean_job_logs
from codebase.conf import settings
from codebase.context import RuntimeCtx  # noqa: TC001
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.tools import BaseTool


logger = logging.getLogger("daiv.tools")


DEFAULT_CLI_TIMEOUT = 30

_PREVIEW_MAX_LINES = 25
_PREVIEW_MAX_CHARS = 1024


def _large_tool_results_prefix(backend: BackendProtocol) -> str:
    """Directory where a full result is written when ``to_file`` is set.

    Derived exactly like ``deepagents`` ``FilesystemMiddleware``'s own eviction prefix:
    ``artifacts_root`` is honoured only for a ``CompositeBackend`` (else ``"/"``). Keeping the
    derivation identical means an explicit ``to_file`` dump lands in the *same* directory, with
    the same ``tool_call_id`` naming, as a result the middleware auto-evicts when it exceeds the
    middleware's tool-result token limit — one convention for the agent to read back via
    ``read_file``/``grep``.
    """
    artifacts_root = backend.artifacts_root if isinstance(backend, CompositeBackend) else "/"
    return f"{artifacts_root.rstrip('/')}/large_tool_results"


def _file_write_confirmation(path: str, byte_count: int, line_count: int, output: str) -> str:
    """Compact confirmation returned in place of the written content: path, size, head preview."""
    preview = "\n".join(output.splitlines()[:_PREVIEW_MAX_LINES])
    if len(preview) > _PREVIEW_MAX_CHARS:
        preview = preview[:_PREVIEW_MAX_CHARS] + "\n… (preview truncated)"
    shown = min(line_count, _PREVIEW_MAX_LINES)
    return f"Wrote {byte_count} bytes ({line_count} lines) to {path}\nPreview (first {shown} lines):\n{preview}"


async def _write_output_to_file(
    output: str,
    *,
    runtime: ToolRuntime[RuntimeCtx],
    backend: BackendProtocol,
    large_tool_results_prefix: str,
    tool_name: str,
) -> str:
    """Write the full result to the large-tool-results dir through the bound filesystem backend.

    Writes via the same backend the agent's ``read_file``/``grep`` tools use, so the file is
    immediately addressable, and keys the path by ``tool_call_id`` exactly like the middleware's
    auto-eviction. Returns a compact confirmation, or an ``error: ...`` string on a failed write.
    """
    if not runtime.tool_call_id:
        # Every result is keyed by tool_call_id (mirroring the middleware's auto-eviction). A
        # missing id is unexpected here; falling back to a shared filename would silently
        # overwrite a previous dump, so fail loudly instead.
        logger.error("[%s] Cannot write result to file: missing tool_call_id", tool_name)
        return "error: Failed to write result to file — missing tool_call_id."
    path = f"{large_tool_results_prefix}/{sanitize_tool_call_id(runtime.tool_call_id)}"
    try:
        result = await backend.awrite(path, output)
    except Exception as exc:
        logger.exception("[%s] Failed to write result to %s", tool_name, path)
        return f"error: Failed to write result to {path}. Details: {exc}"
    if result.error:
        logger.error("[%s] Backend rejected write to %s: %s", tool_name, path, result.error)
        return f"error: Failed to write result to {path}. Details: {result.error}"
    return _file_write_confirmation(path, len(output.encode("utf-8")), len(output.splitlines()), output)


GITLAB_REQUESTS_TIMEOUT = 15
GITLAB_PER_PAGE = "5"
GITLAB_TOOL_NAME = "gitlab"

GITLAB_TOOL_DESCRIPTION = """\
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
- `output_to_file`: set true to write the FULL result to a file (as JSON; `output_mode` is ignored) and get back a compact confirmation instead of the content. Project-job trace is written as raw log text, not JSON.

**Hard rules:**
- Do NOT include the `gitlab` prefix in `subcommand`
- Do NOT pass `--project-id` (the project is injected automatically)
- Do NOT pass `--output` (set automatically from `output_mode`, or forced to json when output_to_file is true)
- Prefer IID-based lookup when available (for example `--iid` for issues and merge requests)

**Default behavior:**
- The configured project is targeted automatically
- Results are ordered from most recent to oldest by default
- List subcommands return the first 5 items by default unless you paginate with `--page`
- A result too large for the context window is automatically saved to a file (see below) instead of being returned inline

**Getting results into a file (for jq/scripts), and automatic saving:**
- Set `output_to_file=true` to write the FULL, untruncated result as JSON (using `--output json`; project-job trace is written as raw log text) instead of returning it inline. The file path is chosen automatically and returned in the confirmation. Then process it with `bash` (jq, scripts, grep) or read it with `read_file`. Use this when you intend to post-process the result.
- This avoids re-typing tool output into a bash command. Pagination stays under your control: pair with `--get-all` or `--per-page` for complete list dumps.
- Even without `output_to_file`, any result too large for the context window is automatically saved to the same large-tool-results directory and replaced inline with a short preview plus the file path — read it with `read_file`, or `grep` within that directory if you don't know the exact path.
- Example: `project-merge-request list --state opened --get-all` with `output_to_file=true`, then read the returned path with `bash`: `jq '[.[].labels[]] | group_by(.) | map({label: .[0], count: length})' <returned-path>`.

**How to use it well:**
- Use `output_mode="simplified"` ONLY for broad `list` discovery where you just need IDs (e.g. `project-merge-request list --state opened`).
- Use `output_mode="detailed"` for every `get --iid`/`--id` call and for any `list` subcommand where you need status/name/stage fields. `simplified` strips almost every field on `get` and is pure overhead when the target is already known.
- Do NOT call a resource in `simplified` mode and then re-call the same resource in `detailed` mode — the second call makes the first redundant. Pick the right mode up front.
- Prefer one targeted query over broad listings.
- If debugging CI, move in this order: pipeline -> failing jobs -> failing job trace.

**Useful subcommand patterns:**
- Issue by IID: `project-issue get --iid <issue_iid>`
- Issue note (comment): `project-issue-note create --issue-iid <issue_iid> --body "<text>"`
- Merge request by IID: `project-merge-request get --iid <merge_request_iid>`
- MR note (comment): `project-merge-request-note create --mr-iid <merge_request_iid> --body "<text>"`
- MR pipelines (only when you need the *history* of pipelines for an MR): `project-merge-request-pipeline list --mr-iid <merge_request_iid>`
  - For the *latest* pipeline status of an MR, do NOT call this — the detailed response of `project-merge-request get --iid <merge_request_iid>` already includes `head_pipeline.status` and `head_pipeline.id`.
- Pipeline jobs (failures only — preferred for CI triage): `project-pipeline-job list --pipeline-id <pipeline_id> --scope failed`
- Pipeline jobs (all): `project-pipeline-job list --pipeline-id <pipeline_id>` (use only when you need to see passing/skipped jobs too, e.g. to diagnose a *skipped* job)
- Job trace: `project-job trace --id <job_id>`
- Filtered issue list: `project-issue list --state opened --labels bug --page <page_number>`
- MR diff versions: `project-merge-request-diff list --mr-iid <merge_request_iid>`
- MR diff detail: `project-merge-request-diff get --mr-iid <merge_request_iid> --id <diff_id>`

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
- For text diff comments, `--position` must be a **JSON object string** containing: `position_type` ("text"), `base_sha`, `start_sha`, `head_sha`, `old_path`, `new_path`, and the correct line anchor:
  - use `new_line` (int) for an added line
  - use `old_line` (int) for a removed line
  - use both `old_line` and `new_line` for an unchanged line
  - Example: `--position '{"position_type": "text", "base_sha": "abc", "start_sha": "def", "head_sha": "ghi", "old_path": "src/foo.py", "new_path": "src/foo.py", "new_line": 42}'`
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
- Always wrap arguments containing spaces or multiline text in double quotes. Do not escape internal quotes with backslashes — use single quotes inside double-quoted strings or rephrase. If the body is very long (>2000 chars), consider splitting into multiple notes.
- When the output is long, extract only the decisive facts needed for the next step"""  # noqa: E501

GITHUB_TOOL_NAME = "gh"

GITHUB_TOOL_DESCRIPTION = """\
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
- `output_to_file`: set true to write the FULL stdout to a file (verbatim) and get back a compact confirmation instead of the content.

**Hard rules:**
- Do NOT include the `gh` prefix in `subcommand`
- Do NOT pass `--repo` or `-R` (the repository is injected automatically)
- Keep listings small and targeted

**Default behavior:**
- The configured repository is targeted automatically
- List subcommands are limited to the first 30 items by default
- Results are ordered from most recent to oldest when supported by the underlying subcommand
- A result too large for the context window is automatically saved to a file (see below) instead of being returned inline

**Getting results into a file (for jq/scripts), and automatic saving:**
- Set `output_to_file=true` to write the FULL, untruncated stdout to a file instead of returning it inline. The file path is chosen automatically and returned in the confirmation. Then process it with `bash` (jq, scripts, grep) or read it with `read_file`. Use this when you intend to post-process the result.
- gh has no global JSON flag — for jq-able output, include gh's own `--json <fields> [--jq ...]` in the subcommand; otherwise the file holds gh's text output.
- Pagination stays under your control: use `--limit` for complete list dumps.
- Even without `output_to_file`, any result too large for the context window is automatically saved to the same large-tool-results directory and replaced inline with a short preview plus the file path — read it with `read_file`, or `grep` within that directory if you don't know the exact path.
- Example: `pr list --state open --json number,title,labels --limit 200` with `output_to_file=true`, then read the returned path with `bash`: `jq '.[] | select(.labels | length == 0) | .number' <returned-path>`.

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

Scope: All operations are scoped to the CURRENT project only. You cannot access files, pipelines, or metadata from other projects. If you need cross-project information, ask the user to provide it.

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
  [Call `{GITLAB_TOOL_NAME}("project-merge-request get --iid 123", output_mode="detailed")`]
  [Read `head_pipeline.status` and `head_pipeline.id` directly from the MR detail — no separate pipeline fetch needed for the latest pipeline]
  [Call `{GITLAB_TOOL_NAME}("project-pipeline-job list --pipeline-id <pipeline_id> --scope failed", output_mode="detailed")`]
  [For each failing job: Call `{GITLAB_TOOL_NAME}("project-job trace --id <job_id>", output_mode="detailed")`]
  [If no jobs come back with --scope failed (pipeline failed because a job was *skipped* or config-rejected), re-list without --scope to inspect job statuses/stages]
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
  [Construct the position as a JSON object with `position_type`, `base_sha`, `start_sha`, `head_sha`, `old_path`, `new_path`, and the correct line field(s)]
  [Call `gitlab("project-merge-request-discussion create --mr-iid 123 --body \\"<comment>\\" --position \\"<json position object>\\"", output_mode="detailed")`]
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


def _parse_gitlab_flag(args: list[str], flag: str) -> str | None:
    """
    Extract a single flag value from a shlex-split args list.

    Supports both `--flag value` and `--flag=value` forms.
    """
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{flag}="):
            return arg[len(flag) + 1 :]
    return None


async def _create_gitlab_inline_discussion(args: list[str], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Create an inline MR diff discussion via the python-gitlab Python API.

    This is the fallback path used when `project-merge-request-discussion create`
    includes `--position`, because the python-gitlab CLI cannot encode nested hash
    parameters (position[base_sha], position[position_type], etc.) correctly.

    Args:
        args: Parsed subcommand arguments after `project-merge-request-discussion create`.
        runtime: The tool runtime carrying the repository context.

    Returns:
        A string suitable for returning from the gitlab tool.
    """
    mr_iid_str = _parse_gitlab_flag(args, "--mr-iid")
    body = _parse_gitlab_flag(args, "--body")
    position_str = _parse_gitlab_flag(args, "--position")

    if not mr_iid_str:
        return "error: --mr-iid is required for inline discussion creation"
    if not body:
        return "error: --body is required for inline discussion creation"
    if not position_str:
        return "error: --position is required for inline discussion creation"

    try:
        mr_iid = int(mr_iid_str)
    except ValueError:
        return f"error: --mr-iid must be an integer, got '{mr_iid_str}'"

    try:
        position = json.loads(position_str)
    except json.JSONDecodeError as e:
        return f"error: --position must be a valid JSON object. Parse error: {e}"

    if not isinstance(position, dict):
        return "error: --position must be a JSON object (dict), not a scalar or list"

    try:
        repo_client = RepoClient.create_instance()
        discussion_id = await asyncio.to_thread(
            repo_client.create_merge_request_inline_discussion, runtime.context.repository.slug, mr_iid, body, position
        )
        return json.dumps({"id": discussion_id, "status": "created"})
    except Exception as e:
        logger.exception("[%s] Failed to create inline MR diff discussion.", GITLAB_TOOL_NAME)
        return f"error: Failed to create inline discussion. Details: {e}"


async def _run_gitlab_subcommand(
    subcommand: str,
    runtime: ToolRuntime[RuntimeCtx],
    output_mode: Literal["detailed", "simplified"],
    to_file: bool,
    *,
    backend: BackendProtocol,
    large_tool_results_prefix: str,
) -> str:
    """
    Run a `python-gitlab` CLI subcommand on behalf of the ``gitlab`` tool.

    Runs a subprocess without shell expansion and keeps the returned output minimal. When
    ``to_file`` is set the full result is written to the large-tool-results dir via ``backend``
    and a compact confirmation is returned; otherwise the output is returned inline and the
    deepagents FilesystemMiddleware evicts it to that same dir if it exceeds the middleware's
    tool-result token limit.
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

    # Inline MR diff discussion: bypass CLI because python-gitlab cannot encode nested
    # hash params (position[base_sha], position[position_type], …) via the CLI.
    if resource == "project-merge-request-discussion" and action == "create":
        rest_args = splitted_subcommand[2:]
        if any(arg == "--position" or arg.startswith("--position=") for arg in rest_args):
            return await _create_gitlab_inline_discussion(rest_args, runtime)

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

    is_job_trace = resource == "project-job" and action == "trace"
    if to_file and not is_job_trace:
        # output_mode (detailed/simplified) only shapes the inline text format for token economy;
        # it is moot for a JSON file dump meant for jq/scripts, so force JSON when writing to file.
        # Job traces are raw streamed log text, not a serializable object — leave them as-is.
        args += ["--output", "json"]
    elif output_mode == "detailed":
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
            logger.warning("[%s] Failed to kill GitLab process: %s", GITLAB_TOOL_NAME, e)
        return "error: GitLab command timed out after 30 seconds. The operation may be too complex or the API is slow."
    except Exception as e:
        logger.exception("[%s] Failed to execute GitLab command.", GITLAB_TOOL_NAME)
        return f"error: Failed to execute GitLab command. Details: {str(e)}"

    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8").strip()
        return f"error: GitLab command failed (exit code {process.returncode}). Details: {stderr_text}"

    output = stdout.decode("utf-8").strip()
    if not output:
        # This branch is reached only after returncode == 0 (failures return an `error: ...`
        # string above), so "no file was written" provably means a genuinely empty result, not a
        # failed command. The code-review skill's `gitlab-delivery.md` Step 1 keys its
        # empty-listing handling off this exact "no file was written" phrasing — keep them in sync.
        empty = "(empty result — command succeeded with no output, e.g. an empty list or no matches)"
        if to_file:
            empty += "\n(note: no file was written — the command produced no output.)"
        return empty

    if is_job_trace:
        output = clean_job_logs(output, runtime.context.git_platform)

    if to_file:
        return await _write_output_to_file(
            output,
            runtime=runtime,
            backend=backend,
            large_tool_results_prefix=large_tool_results_prefix,
            tool_name=GITLAB_TOOL_NAME,
        )
    return output


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


async def _run_github_subcommand(
    subcommand: str,
    runtime: ToolRuntime[RuntimeCtx],
    to_file: bool,
    *,
    backend: BackendProtocol,
    large_tool_results_prefix: str,
) -> str | Command:
    """
    Run a `gh` CLI subcommand on behalf of the ``gh`` tool.

    Runs a subprocess without shell expansion and keeps the returned output minimal. When
    ``to_file`` is set the full stdout is written to the large-tool-results dir via ``backend``
    and a compact confirmation is returned; otherwise the output is returned inline and the
    deepagents FilesystemMiddleware evicts it to that same dir if it exceeds the middleware's
    tool-result token limit.
    ``gh`` has no global JSON flag, so for jq-able output include ``--json <fields>`` in the
    subcommand. Returns a ``Command`` when a refreshed CLI token must be written back to state.
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
    # The "api" resource is intentionally excluded from GITHUB_CLI_ALLOW_COMMANDS
    # (and therefore blocked by _is_allowed_cli_command above) because it would
    # bypass the --repo scoping applied below. If "api" were ever allowed, it
    # must NOT receive the --repo flag and would need its own access controls.
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
            logger.warning("[%s] Failed to kill GitHub process: %s", GITHUB_TOOL_NAME, e)

        return "error: GitHub command timed out after 30 seconds. The operation may be too complex or the API is slow."

    except Exception as e:
        logger.exception("[%s] Failed to execute GitHub command.", GITHUB_TOOL_NAME)
        return f"error: Failed to execute GitHub command. Details: {str(e)}"

    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8").strip()
        return f"error: GitHub command failed (exit code {process.returncode}). Details: {stderr_text}"

    output = stdout.decode("utf-8").strip()
    if not output:
        final_output = "(empty result — command succeeded with no output, e.g. an empty list or no matches)"
        if to_file:
            final_output += "\n(note: no file was written — the command produced no output.)"
    else:
        if resource == "run" and action == "view" and "--log" in splitted_subcommand:
            output = clean_job_logs(output, runtime.context.git_platform)

        if to_file:
            final_output = await _write_output_to_file(
                output,
                runtime=runtime,
                backend=backend,
                large_tool_results_prefix=large_tool_results_prefix,
                tool_name=GITHUB_TOOL_NAME,
            )
        else:
            final_output = output

    # Return Command with state update if token was cached/refreshed
    if state_update:
        tool_message = ToolMessage(content=final_output, tool_call_id=runtime.tool_call_id)
        state_update["messages"] = [tool_message]
        return Command(update=state_update)

    return final_output


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
        from codebase.base import GitPlatform
        from automation.agent.middlewares.git_platform import GitPlatformMiddleware

        store = InMemoryStore()

        agent = create_agent(
            model="openai:gpt-4o",
            # ``backend`` is the agent's filesystem backend (e.g. the shared SandboxFileBackend).
            middleware=[GitPlatformMiddleware(git_platform=GitPlatform.GITHUB, backend=backend)],
            store=store,
        )
        ```
    """

    def __init__(self, git_platform: GitPlatform, backend: BackendProtocol) -> None:
        """
        Initialize the middleware.

        ``backend`` is the same filesystem backend the agent's file tools use; the platform tools
        write ``to_file`` dumps through it, into the same large-tool-results dir the deepagents
        FilesystemMiddleware auto-evicts to (derived identically from the backend's artifacts root).
        """
        super().__init__()

        self._backend = backend
        self._large_tool_results_prefix = _large_tool_results_prefix(backend)

        self.tools = []

        if git_platform == GitPlatform.GITLAB:
            self.tools.append(self._build_gitlab_tool())
        elif git_platform == GitPlatform.GITHUB:
            self.tools.append(self._build_github_tool())

    def _build_gitlab_tool(self) -> BaseTool:
        """Build the ``gitlab`` tool as a closure over the bound backend + results prefix."""
        backend = self._backend
        large_tool_results_prefix = self._large_tool_results_prefix

        @tool(GITLAB_TOOL_NAME, description=GITLAB_TOOL_DESCRIPTION)
        async def gitlab(
            subcommand: Annotated[
                str,
                "Single GitLab CLI subcommand string, parsed with shell-like quoting. "
                "Format: '<object> <action> [arguments...]'. "
                "Do not include the 'gitlab' prefix, '--project-id', or any output/verbosity flags "
                "managed by the tool. Wrap arguments containing spaces in double quotes. "
                "Examples: 'project-issue get --iid 42', 'project-merge-request list --state opened'.",
            ],
            runtime: ToolRuntime[RuntimeCtx],
            output_mode: Annotated[
                Literal["detailed", "simplified"],
                "Controls the detail level of the returned output. "
                "Use 'simplified' for listing/discovery and 'detailed' for inspecting a specific "
                "resource or reading logs. Ignored when output_to_file is true (JSON is forced). "
                "Default: 'simplified'.",
            ] = "simplified",
            output_to_file: Annotated[
                bool,
                "Set true to write the FULL result to a file for bash/jq/grep instead of returning it "
                "inline. The path is auto-assigned under the large-tool-results dir and returned in a "
                "compact confirmation (path + size + short preview). The result is written as JSON "
                "(output_mode ignored); a project-job trace is written as raw log text. Use this when "
                "you intend to post-process the result with bash. Default: false.",
            ] = False,
        ) -> str:
            return await _run_gitlab_subcommand(
                subcommand,
                runtime,
                output_mode,
                output_to_file,
                backend=backend,
                large_tool_results_prefix=large_tool_results_prefix,
            )

        return gitlab

    def _build_github_tool(self) -> BaseTool:
        """Build the ``gh`` tool as a closure over the bound backend + results prefix."""
        backend = self._backend
        large_tool_results_prefix = self._large_tool_results_prefix

        @tool(GITHUB_TOOL_NAME, description=GITHUB_TOOL_DESCRIPTION)
        async def github(
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
            output_to_file: Annotated[
                bool,
                "Set true to write the FULL stdout to a file for bash/jq/grep instead of returning it "
                "inline. The path is auto-assigned under the large-tool-results dir and returned in a "
                "compact confirmation (path + size + short preview). gh has no global JSON flag — for "
                "jq-able output include gh's own --json <fields> [--jq ...] in the subcommand. Use this "
                "when you intend to post-process the result with bash. Default: false.",
            ] = False,
        ) -> str | Command:
            return await _run_github_subcommand(
                subcommand,
                runtime,
                output_to_file,
                backend=backend,
                large_tool_results_prefix=large_tool_results_prefix,
            )

        return github

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
