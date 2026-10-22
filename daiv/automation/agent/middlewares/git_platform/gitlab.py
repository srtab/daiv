from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import TYPE_CHECKING, Literal

from automation.agent.middlewares.git_platform.cli import (
    AllowCommands,
    CliInvocation,
    PlatformCli,
    check_subcommand,
    run_cli,
)
from codebase.base import GitPlatform
from codebase.clients.utils import clean_job_logs
from codebase.conf import settings
from daiv import USER_AGENT

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain.tools import ToolRuntime

    from automation.agent.context import RuntimeCtx


logger = logging.getLogger("daiv.tools")


GITLAB_REQUESTS_TIMEOUT = 15
GITLAB_PER_PAGE = "5"
GITLAB_TOOL_NAME = "gitlab"


GITLAB_CLI_ALLOW_COMMANDS: AllowCommands = {
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


GITLAB_CLI = PlatformCli(
    display_name="GitLab",
    tool_name=GITLAB_TOOL_NAME,
    executable="gitlab",
    usage="<object> <action> <arguments>",
    example="project-issue get --iid 123",
    allow_commands=GITLAB_CLI_ALLOW_COMMANDS,
    has_disallowed_flags=_gitlab_has_disallowed_cli_flags,
    disallowed_flags_error="error: The project ID and output format are automatically set.",
)


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
        discussion_id = await asyncio.to_thread(
            runtime.context.repo.client.create_merge_request_inline_discussion,
            runtime.context.repository.slug,
            mr_iid,
            body,
            position,
        )
        return json.dumps({"id": discussion_id, "status": "created"})
    except Exception as e:
        logger.exception("[%s] Failed to create inline MR diff discussion.", GITLAB_TOOL_NAME)
        return f"error: Failed to create inline discussion. Details: {e}"


async def run_gitlab_subcommand(
    subcommand: str,
    runtime: ToolRuntime[RuntimeCtx],
    output_mode: Literal["detailed", "simplified"],
    to_file: bool,
    *,
    backend: BackendProtocol,
) -> str:
    """Run a `python-gitlab` CLI subcommand on behalf of the ``gitlab`` tool, scoped to the run's project."""
    args = check_subcommand(GITLAB_CLI, subcommand)
    if isinstance(args, str):
        return args

    resource, action, rest = args[0], args[1], args[2:]
    has_position = any(arg == "--position" or arg.startswith("--position=") for arg in rest)
    if resource == "project-merge-request-discussion" and action == "create" and has_position:
        return await _create_gitlab_inline_discussion(rest, runtime)

    is_job_trace = resource == "project-job" and action == "trace"
    # output_mode only shapes inline text; a file dump is for jq, so JSON, except a trace, which is raw log text.
    if to_file and not is_job_trace:
        output_flags = ["--output", "json"]
    elif output_mode == "detailed":
        output_flags = ["--verbose"]
    else:
        output_flags = []

    project_flag = "--id" if resource == "project" else "--project-id"
    invocation = CliInvocation(
        args=[*output_flags, *args, project_flag, runtime.context.repository.slug],
        env={
            "GITLAB_TIMEOUT": str(GITLAB_REQUESTS_TIMEOUT),
            "GITLAB_PRIVATE_TOKEN": settings.GITLAB_AUTH_TOKEN.get_secret_value(),
            "GITLAB_URL": settings.GITLAB_URL.encoded_string(),
            "GITLAB_PER_PAGE": GITLAB_PER_PAGE,
            "GITLAB_USER_AGENT": USER_AGENT,
        },
        postprocess=partial(clean_job_logs, git_platform=GitPlatform.GITLAB) if is_job_trace else None,
    )
    return await run_cli(GITLAB_CLI, invocation, runtime, to_file=to_file, backend=backend)
