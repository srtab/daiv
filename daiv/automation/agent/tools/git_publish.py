from __future__ import annotations

import logging
from typing import Annotated

from django.template.loader import render_to_string
from django.utils.text import slugify

from asgiref.sync import sync_to_async
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from automation.agent.git_utils import open_git_manager
from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.context import RuntimeCtx  # noqa: TC001
from core.constants import BOT_LABEL, BOT_NAME

logger = logging.getLogger("daiv.tools")

COMMIT_CHANGES_NAME = "commit_changes"
CREATE_MERGE_REQUEST_NAME = "create_merge_request"

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
changes are pushed to that branch and the existing MR/PR is updated instead of creating a new one.

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
        await git_manager.commit_all(message)

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
    merge_request = await sync_to_async(client.update_or_create_merge_request)(
        repo_id=ctx.repository.slug,
        source_branch=source_branch,
        target_branch=ctx.config.default_branch,
        title=title,
        description=rendered_description,
        labels=[BOT_LABEL],
        assignee_id=_resolve_assignee_id(ctx),
        as_draft=False,
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
