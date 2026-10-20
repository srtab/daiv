"""Start an addressor run for a webhook event that a platform's callback has parsed and accepted.

Each dispatch reacts with 👀 on what triggered it, opens the issue's or merge request's ``Session``, enqueues the
addressor task, then records the ``Run`` linked one-to-one to the task result. The session exists before the task
can run, so every addressor run waits for the session's slot, its first included. The repository, iid and mentioning
note are the task's ``dedup_on`` key: a repeated delivery or an edited comment gets the earlier trigger's task back,
which already has its ``Run``, and stops there. The backend tells the two apart under the lock it dedups with, so of
two concurrent deliveries only the one that created the task records a ``Run``.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from github.GithubException import GithubException
from gitlab.exceptions import GitlabError
from sandbox_envs.selection import resolve_env_for_run
from sessions.models import SessionOrigin
from sessions.services import acreate_run, aget_or_create_session, pin_max_model

from accounts.utils import resolve_user
from codebase.base import GitPlatform, Scope
from codebase.clients.base import Emoji
from codebase.utils import compute_thread_id
from webhooks.tasks import address_issue_task, address_mr_comments_task

if TYPE_CHECKING:
    from django_tasks import Task

    from codebase.clients import RepoClient

logger = logging.getLogger("daiv.webhooks")


@dataclass(frozen=True)
class Author:
    """The platform account that triggered the event."""

    provider: GitPlatform
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
    note_id, comment_id = (mention.note_id, mention.comment_id) if mention else (None, None)
    try:
        await sync_to_async(client.create_issue_emoji)(repo_id, issue_iid, Emoji.EYES, note_id)
    except GitlabError, GithubException, OSError:
        logger.exception("Failed to add reaction to issue %s#%s", repo_id, issue_iid)

    thread_id, sandbox_environment_id = await _session_keys(repo_id, Scope.ISSUE, issue_iid)
    await _start_run(
        address_issue_task,
        {"issue_iid": issue_iid, "mention_comment_id": comment_id, "mention_note_id": note_id},
        author,
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        repo_id=repo_id,
        thread_id=thread_id,
        sandbox_environment_id=sandbox_environment_id,
        use_max=use_max,
        title=title,
        issue_iid=issue_iid,
        mention_comment_id=comment_id or "",
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
    it is then fetched, and left blank if that fails, since the run is still worth starting.
    """
    try:
        await sync_to_async(client.create_merge_request_note_emoji)(
            repo_id, merge_request_iid, Emoji.EYES, mention.note_id
        )
    except GitlabError, GithubException, OSError:
        logger.exception("Failed to add reaction to merge request comment %s#%s", repo_id, merge_request_iid)

    thread_id, sandbox_environment_id = await _session_keys(repo_id, Scope.MERGE_REQUEST, merge_request_iid)
    if ref is None:
        ref = await _fetch_source_branch(client, repo_id, merge_request_iid)
    await _start_run(
        address_mr_comments_task,
        {
            "merge_request_id": merge_request_iid,
            "mention_comment_id": mention.comment_id,
            "mention_note_id": mention.note_id,
        },
        author,
        trigger_type=SessionOrigin.MR_WEBHOOK,
        repo_id=repo_id,
        thread_id=thread_id,
        sandbox_environment_id=sandbox_environment_id,
        use_max=use_max,
        title=title,
        ref=ref,
        merge_request_iid=merge_request_iid,
        mention_comment_id=mention.comment_id,
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


async def _start_run(
    task: Task,
    task_kwargs: dict[str, Any],
    author: Author,
    *,
    trigger_type: SessionOrigin,
    repo_id: str,
    thread_id: str,
    sandbox_environment_id: str | None,
    use_max: bool,
    title: str,
    ref: str = "",
    issue_iid: int | None = None,
    merge_request_iid: int | None = None,
    mention_comment_id: str = "",
) -> None:
    """Open the session, enqueue ``task`` and record its ``Run``, unless the enqueue handed back an earlier trigger's
    task, which already has its ``Run``. ``task_kwargs`` are the task's own; the session keys and ``use_max`` are added.

    A failed session write raises before anything is queued. A failed ``Run`` insert is only logged: the task is
    queued by then and runs without one.
    """
    user = await resolve_user(author.provider, author.uid, username=author.username, email=author.email)
    agent_model, agent_thinking_level = pin_max_model(use_max)
    session = await aget_or_create_session(
        thread_id=thread_id,
        origin=trigger_type,
        repo_id=repo_id,
        ref=ref,
        user=user,
        external_username=author.username,
        title=title,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
        sandbox_environment_id=sandbox_environment_id,
        issue_iid=issue_iid,
        merge_request_iid=merge_request_iid,
    )
    result, created = await task.aget_or_enqueue(
        repo_id=repo_id,
        thread_id=thread_id,
        sandbox_environment_id=sandbox_environment_id,
        use_max=use_max,
        **task_kwargs,
    )
    if not created:
        logger.info("Task result %s already has its run; skipping the repeated trigger", result.id)
        return
    try:
        await acreate_run(
            session=session,
            task_result_id=result.id,
            trigger_type=trigger_type,
            repo_id=repo_id,
            ref=ref,
            use_max=use_max,
            merge_request_iid=merge_request_iid,
            mention_comment_id=mention_comment_id,
            user=user,
            external_username=author.username,
            title=title,
            sandbox_environment_id=sandbox_environment_id,
        )
    except Exception:
        logger.exception("Failed to create run for task result %s on %s", result.id, repo_id)
