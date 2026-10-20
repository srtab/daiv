"""Start an addressor run for a webhook event that a platform's callback has parsed and accepted.

Each dispatch reacts with 👀 on what triggered it, enqueues the addressor task, then records the ``Run`` linked
one-to-one to the task result. The repository, iid and mentioning note are the task's ``dedup_on`` key: a repeated
delivery or an edited comment gets the earlier trigger's task back, which already has its ``Run``, and stops there.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from asgiref.sync import sync_to_async
from github.GithubException import GithubException
from gitlab.exceptions import GitlabError
from sandbox_envs.selection import resolve_env_for_run
from sessions.models import Run, SessionOrigin
from sessions.services import acreate_run

from accounts.utils import resolve_user
from codebase.base import Scope
from codebase.clients.base import Emoji
from codebase.utils import compute_thread_id
from webhooks.tasks import address_issue_task, address_mr_comments_task

if TYPE_CHECKING:
    from codebase.clients import RepoClient

logger = logging.getLogger("daiv.webhooks")


@dataclass(frozen=True)
class Author:
    """The platform account that triggered the event."""

    provider: Literal["gitlab", "github"]
    uid: int
    username: str
    email: str = ""


@dataclass(frozen=True)
class Mention:
    """A comment that mentioned DAIV."""

    note_id: int
    """The comment DAIV reacts on, and the one run it may start."""

    comment_id: str
    """What the addressor reads and answers: GitLab's discussion id, GitHub's comment id."""


async def dispatch_issue_run(
    *,
    client: RepoClient,
    repo_id: str,
    issue_iid: int,
    title: str,
    use_max: bool,
    author: Author,
    mention: Mention | None = None,
) -> None:
    """Address an issue, triggered by its DAIV label or, with ``mention``, by a comment on it."""
    try:
        await sync_to_async(client.create_issue_emoji)(
            repo_id, issue_iid, Emoji.EYES, mention.note_id if mention else None
        )
    except GitlabError, GithubException, OSError:
        logger.exception("Failed to add reaction to issue %s#%s", repo_id, issue_iid)

    thread_id, sandbox_environment_id = await _session_keys(repo_id, Scope.ISSUE, issue_iid)
    result = await address_issue_task.aenqueue(
        repo_id=repo_id,
        issue_iid=issue_iid,
        mention_comment_id=mention.comment_id if mention else None,
        mention_note_id=mention.note_id if mention else None,
        thread_id=thread_id,
        sandbox_environment_id=sandbox_environment_id,
        use_max=use_max,
    )
    if await _is_repeated_trigger(result.id):
        return
    await _record_run(
        result.id,
        author,
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        repo_id=repo_id,
        issue_iid=issue_iid,
        mention_comment_id=mention.comment_id if mention else "",
        use_max=use_max,
        title=title,
        thread_id=thread_id,
        sandbox_environment_id=sandbox_environment_id,
    )


async def dispatch_mr_comment_run(
    *,
    client: RepoClient,
    repo_id: str,
    merge_request_iid: int,
    ref: str | None,
    title: str,
    use_max: bool,
    author: Author,
    mention: Mention,
) -> None:
    """Address a comment that mentioned DAIV on a merge request.

    ``ref`` is the merge request's source branch, ``None`` when the payload omits it (GitHub's ``issue_comment``);
    it is then fetched after the enqueue, and left blank if that fails, since the run is still worth recording.
    """
    try:
        await sync_to_async(client.create_merge_request_note_emoji)(
            repo_id, merge_request_iid, Emoji.EYES, mention.note_id
        )
    except GitlabError, GithubException, OSError:
        logger.exception("Failed to add reaction to merge request comment %s#%s", repo_id, merge_request_iid)

    thread_id, sandbox_environment_id = await _session_keys(repo_id, Scope.MERGE_REQUEST, merge_request_iid)
    result = await address_mr_comments_task.aenqueue(
        repo_id=repo_id,
        merge_request_id=merge_request_iid,
        mention_comment_id=mention.comment_id,
        mention_note_id=mention.note_id,
        thread_id=thread_id,
        sandbox_environment_id=sandbox_environment_id,
        use_max=use_max,
    )
    if await _is_repeated_trigger(result.id):
        return
    if ref is None:
        ref = await _fetch_source_branch(client, repo_id, merge_request_iid)
    await _record_run(
        result.id,
        author,
        trigger_type=SessionOrigin.MR_WEBHOOK,
        repo_id=repo_id,
        ref=ref,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention.comment_id,
        use_max=use_max,
        title=title,
        thread_id=thread_id,
        sandbox_environment_id=sandbox_environment_id,
    )


async def _session_keys(repo_id: str, scope: Scope, entity_iid: int) -> tuple[str, str | None]:
    """The thread id every event on this issue or merge request shares, and the sandbox environment to run in."""
    thread_id = compute_thread_id(repo_slug=repo_id, scope=scope, entity_iid=entity_iid)
    # user=None: the webhook fires for whoever touched the issue/MR, not the agent owner, so USER envs never apply.
    sandbox_env = await resolve_env_for_run(user=None, repo_id=repo_id)
    return thread_id, str(sandbox_env.id) if sandbox_env is not None else None


async def _fetch_source_branch(client: RepoClient, repo_id: str, merge_request_iid: int) -> str:
    try:
        merge_request = await sync_to_async(client.get_merge_request)(repo_id, merge_request_iid)
    except Exception:
        logger.exception("Failed to resolve source branch for merge request %s#%s", repo_id, merge_request_iid)
        return ""
    return merge_request.source_branch


async def _is_repeated_trigger(task_result_id: str) -> bool:
    """Whether the deduplicated enqueue handed back an earlier trigger's task, which already has its ``Run``."""
    if await Run.objects.filter(task_result_id=task_result_id).aexists():
        logger.info("Task result %s already has its run; skipping the repeated trigger", task_result_id)
        return True
    return False


async def _record_run(task_result_id: str, author: Author, **run_kwargs: Any) -> None:
    user = await resolve_user(author.provider, author.uid, username=author.username, email=author.email)
    try:
        await acreate_run(task_result_id=task_result_id, user=user, external_username=author.username, **run_kwargs)
    except Exception:
        logger.exception("Failed to create run for task result %s on %s", task_result_id, run_kwargs["repo_id"])
