from __future__ import annotations

import logging
from typing import Annotated, cast

from django.template.loader import render_to_string
from django.utils.text import slugify

import httpx
from asgiref.sync import sync_to_async
from git import GitCommandError
from github import GithubException
from gitlab.exceptions import GitlabError
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from automation.agent.git_utils import open_git_manager
from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import GitPushNetworkError, GitPushPermissionError
from core.constants import BOT_LABEL, BOT_NAME

logger = logging.getLogger("daiv.tools")

COMMIT_CHANGES_NAME = "commit_changes"
CREATE_MERGE_REQUEST_NAME = "create_merge_request"

# Commit/push failures we translate into a tool-visible message so the agent can explain or
# recover, rather than crashing the run with an uncaught exception.
_PUSH_FAILURES = (GitCommandError, GitPushPermissionError, GitPushNetworkError)
# Platform/transport errors from the MR API call. Bugs (KeyError, AttributeError, …) still
# propagate so the run fails loudly rather than masking a defect as a tool error.
_MR_API_FAILURES = (GitlabError, GithubException, httpx.HTTPError)

COMMIT_CHANGES_DESCRIPTION = """\
Commit the changes you have made in the workspace so far (`git add -A` + `git commit`).

Use this when a logical unit of work is ready. Write a clear, conventional commit message
(e.g. `fix: handle empty payload`). You may call this multiple times to build up a series of
commits. Committing does NOT publish anything — open the merge/pull request with
`create_merge_request` when the change is ready for review.

Returns a short confirmation. If the working tree is clean, it reports that there was
nothing to commit (not an error).
"""

CREATE_MERGE_REQUEST_DESCRIPTION = """\
Push your committed changes and open (or update) the merge/pull request for this work.

Provide a clear `title` and a `description` (markdown) authored by you — these become the
MR/PR title and body. Optionally pass `branch` to control the source branch name; otherwise
one is derived from the title. When this run is already attached to an open MR/PR, the
changes are pushed to that branch and the existing MR/PR is updated instead of creating a new
one — in that case `branch` is ignored (the existing source branch is reused).

Any still-uncommitted changes are committed first (using the title as the message) so no work
is lost. Returns the merge/pull request URL.
"""


def _branch_from_title(title: str) -> str:
    """Derive a branch name from an MR title; fall back to a stable default for empty slugs."""
    return slugify(title)[:60].strip("-") or "daiv-changes"


def _resolve_assignee_id(ctx: RuntimeCtx) -> str | int | None:
    """Assignee for a newly created MR: the triggering issue's assignee, if any."""
    if ctx.issue and ctx.issue.assignee:
        return ctx.issue.assignee.id if ctx.git_platform == GitPlatform.GITLAB else ctx.issue.assignee.username
    return None


def _tool_error(runtime: ToolRuntime[RuntimeCtx], message: str) -> Command:
    """Return a Command carrying an error ``ToolMessage`` so the agent sees (and can act on) a
    failure as a tool result instead of the run crashing on an uncaught exception."""
    return Command(
        update={
            "messages": [ToolMessage(content=f"error: {message}", tool_call_id=runtime.tool_call_id, status="error")]
        }
    )


@tool(COMMIT_CHANGES_NAME, description=COMMIT_CHANGES_DESCRIPTION)
async def commit_changes(
    message: Annotated[str, "A clear, conventional commit message for the staged changes."],
    runtime: ToolRuntime[RuntimeCtx],
) -> Command | str:
    """Stage and commit the current workspace changes."""
    session_id = (runtime.state or {}).get("session_id")
    async with open_git_manager(session_id=session_id, gitrepo=runtime.context.gitrepo) as git_manager:
        if not await git_manager.is_dirty():
            return "Nothing to commit: the working tree is clean."
        try:
            await git_manager.commit_all(message)
        except GitCommandError as exc:
            logger.warning("commit_changes: git commit failed: %s", exc)
            return _tool_error(
                runtime,
                f"Commit failed: {exc}. The working tree is unchanged; resolve the cause (e.g. a "
                f"rejecting pre-commit hook) and try again.",
            )

    return Command(
        update={
            "code_changes": True,
            "messages": [ToolMessage(content=f"Committed changes: {message}", tool_call_id=runtime.tool_call_id)],
        }
    )


@tool(CREATE_MERGE_REQUEST_NAME, description=CREATE_MERGE_REQUEST_DESCRIPTION)
async def create_merge_request(
    title: Annotated[str, "The merge/pull request title."],
    description: Annotated[str, "The merge/pull request description (markdown)."],
    runtime: ToolRuntime[RuntimeCtx],
    branch: Annotated[str | None, "Optional source branch name; derived from the title if omitted."] = None,
) -> Command:
    """Push the committed changes and open or update the merge/pull request."""
    ctx = runtime.context
    session_id = (runtime.state or {}).get("session_id")
    existing_mr = (runtime.state or {}).get("merge_request")
    client = RepoClient.create_instance()

    try:
        async with open_git_manager(session_id=session_id, gitrepo=ctx.gitrepo) as git_manager:
            # Fold any still-uncommitted work into the MR so nothing is lost.
            if await git_manager.is_dirty():
                await git_manager.commit_all(title)

            if existing_mr is not None:
                # Continuing an existing MR: push onto its source branch and update it.
                source_branch = existing_mr.source_branch
            else:
                source_branch = (branch.strip() if branch else "") or _branch_from_title(title)
                source_branch = git_manager.unique_branch_name(source_branch, await git_manager.remote_branches())

            await git_manager.push_head_to(source_branch)
    except _PUSH_FAILURES as exc:
        logger.warning("create_merge_request: could not commit/push changes: %s", exc)
        return _tool_error(runtime, f"Could not publish the changes: {exc}")

    rendered_description = render_to_string(
        "automation/issue_merge_request.txt",
        {
            "description": description,
            "source_repo_id": ctx.repository.slug,
            "issue_id": ctx.issue.iid if ctx.issue else None,
            "bot_name": BOT_NAME,
            "bot_username": ctx.bot_username,
            "is_gitlab": ctx.git_platform == GitPlatform.GITLAB,
            "fallback_from_mr": None,
        },
    )
    try:
        merge_request = await sync_to_async(client.update_or_create_merge_request)(
            repo_id=ctx.repository.slug,
            source_branch=source_branch,
            target_branch=cast("str", ctx.config.default_branch),
            title=title,
            description=rendered_description,
            labels=[BOT_LABEL],
            assignee_id=_resolve_assignee_id(ctx),
            as_draft=False,
        )
    except _MR_API_FAILURES as exc:
        # The changes are already pushed; tell the agent so it can surface a recoverable state
        # (the branch exists on the remote) rather than dying with a raw platform exception.
        logger.warning("create_merge_request: MR API call failed after pushing '%s': %s", source_branch, exc)
        return _tool_error(
            runtime,
            f"Changes were pushed to branch '{source_branch}', but opening/updating the merge request failed: {exc}",
        )
    logger.info("Agent opened/updated merge request %s on branch '%s'", merge_request.web_url, source_branch)

    return Command(
        update={
            "merge_request": merge_request,
            "code_changes": True,
            "messages": [
                ToolMessage(content=f"Merge request ready: {merge_request.web_url}", tool_call_id=runtime.tool_call_id)
            ],
        }
    )
