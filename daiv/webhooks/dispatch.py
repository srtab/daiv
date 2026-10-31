"""Start an addressor run for a webhook event that a platform's callback has parsed and accepted.

A comment on a merge request a session owns goes to that session's inbox instead: a mention as a waking event, which
gets the 👀 reaction once, any other comment as a remembered one. Only an owned merge request takes a mention on a
diff line.

Otherwise each dispatch enqueues the addressor task and records its ``Run`` on the issue's or merge request's
``Session`` in one transaction, then reacts with 👀 on what triggered it. A worker sees the task only once its session
and ``Run`` exist, so every addressor run waits for the session's slot and knows its ``Run``. The repository, iid and
mentioning note are the task's ``dedup_on`` key: a repeated delivery or an edited comment finds the earlier trigger's
task, which has its ``Run`` already, and stops without writing or reacting. A label trigger only repeats a queued or
running one. The enqueue holds the key's lock until the transaction commits, so of concurrent deliveries exactly one
creates the task and its ``Run``.
"""

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from django.db import transaction

from asgiref.sync import sync_to_async
from sandbox_envs.selection import resolve_env_for_run
from sessions.ids import compute_thread_id
from sessions.inbox import kinds
from sessions.inbox.ownership import aowning_session
from sessions.inbox.post import apost_event
from sessions.models import EventKind, SessionOrigin
from sessions.services import create_run

from accounts.utils import resolve_user
from codebase.base import GitPlatform, Scope
from codebase.clients.base import PLATFORM_ERRORS, Emoji
from core.site_settings import site_settings
from webhooks.tasks import address_issue_label_task, address_issue_task, address_mr_comments_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from sessions.models import Session

    from codebase.clients import RepoClient
    from core.backends.deduplicating import DeduplicatingTask

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
    """A comment that mentioned DAIV or, on a merge request a session owns, any comment."""

    note_id: int
    """The comment DAIV reacts on, the one run it may start, and the note that run answers."""

    comment_id: str
    """The discussion holding it, which the addressor reads and replies under: GitLab's discussion id, GitHub's
    comment id, or for a GitHub review comment the id of the first comment in its thread."""

    body: str = ""
    web_url: str = ""
    on_diff: bool = False
    """A GitLab DiffNote or a GitHub review comment."""


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
    started = await _start_run(
        address_issue_task if mention else address_issue_label_task,
        author,
        repo_id=repo_id,
        scope=Scope.ISSUE,
        iid=issue_iid,
        use_max=use_max,
        task_kwargs={"issue_iid": issue_iid, "mention_comment_id": comment_id, "mention_note_id": note_id},
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        title=title,
    )
    if started:
        await _react(
            partial(client.create_issue_emoji, repo_id, issue_iid, Emoji.EYES, note_id), f"issue {repo_id}#{issue_iid}"
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
    """Address a comment that mentioned DAIV on a merge request, in the session that owns it when one does.

    ``ref`` is the merge request's source branch, ``None`` when the payload omits it (GitHub's ``issue_comment``);
    it is then fetched, and left blank if that fails, since the run is still worth starting.
    """
    if (owner := await aowning_session(repo_id, merge_request_iid, title=title)) is not None:
        if await _post_comment(owner, client, merge_request_iid, author, mention, kind=EventKind.MENTION):
            await _react(
                partial(
                    client.create_merge_request_note_emoji,
                    repo_id,
                    merge_request_iid,
                    Emoji.EYES,
                    mention.note_id,
                    on_diff=mention.on_diff,
                ),
                f"merge request comment {repo_id}#{merge_request_iid}",
            )
        return
    if mention.on_diff:
        logger.info("Ignoring a diff mention on %s#%s: no session owns it", repo_id, merge_request_iid)
        return
    if ref is None:
        ref = await _fetch_source_branch(client, repo_id, merge_request_iid)
    started = await _start_run(
        address_mr_comments_task,
        author,
        repo_id=repo_id,
        scope=Scope.MERGE_REQUEST,
        iid=merge_request_iid,
        use_max=use_max,
        task_kwargs={
            "merge_request_id": merge_request_iid,
            "mention_comment_id": mention.comment_id,
            "mention_note_id": mention.note_id,
        },
        trigger_type=SessionOrigin.MR_WEBHOOK,
        title=title,
        ref=ref,
    )
    if started:
        await _react(
            partial(client.create_merge_request_note_emoji, repo_id, merge_request_iid, Emoji.EYES, mention.note_id),
            f"merge request comment {repo_id}#{merge_request_iid}",
        )


async def dispatch_mr_comment(
    *, client: RepoClient, repo_id: str, merge_request_iid: int, title: str, author: Author, comment: Mention
) -> None:
    """Remember a comment that did not mention DAIV in the session that owns the merge request; with no such session
    it concerns no one."""
    if (owner := await aowning_session(repo_id, merge_request_iid, title=title)) is not None:
        await _post_comment(owner, client, merge_request_iid, author, comment, kind=EventKind.COMMENT)


async def _post_comment(
    session: Session, client: RepoClient, merge_request_iid: int, author: Author, comment: Mention, *, kind: str
) -> bool:
    """Post ``comment`` to ``session``'s inbox; returns whether it is new, since an edit keeps the comment's key."""
    review_comment = comment.on_diff and client.git_platform == GitPlatform.GITHUB
    payload: dict[str, Any] = {
        "body": comment.body,
        "web_url": comment.web_url,
        "discussion_id": comment.comment_id,
        "is_diff": comment.on_diff,
        ("review_comment_id" if review_comment else "note_id"): comment.note_id,
    }
    user = await resolve_user(author.provider, author.uid, username=author.username, email=author.email)
    result = await apost_event(
        session.thread_id,
        kind=kind,
        source_key=kinds.review_comment_key(comment.note_id) if review_comment else kinds.note_key(comment.note_id),
        payload=payload,
        wake=kind == EventKind.MENTION,
        merge_request_iid=merge_request_iid,
        author=user,
        author_username=author.username,
    )
    return result.created


def _pinned_model(use_max: bool) -> tuple[str, str]:
    """The agent model and thinking level to record: the site's max ones for ``daiv-max``, else none."""
    if use_max:
        return site_settings.agent_max_model_name, site_settings.agent_max_thinking_level
    return "", ""


async def _fetch_source_branch(client: RepoClient, repo_id: str, merge_request_iid: int) -> str:
    try:
        merge_request = await sync_to_async(client.get_merge_request)(repo_id, merge_request_iid)
    except Exception:
        logger.exception("Failed to resolve source branch for merge request %s#%s", repo_id, merge_request_iid)
        return ""
    return merge_request.source_branch


async def _react(add_reaction: Callable[[], object], target: str) -> None:
    try:
        await sync_to_async(add_reaction)()
    except PLATFORM_ERRORS:
        logger.exception("Failed to add reaction to %s", target)


async def _start_run(
    task: DeduplicatingTask,
    author: Author,
    *,
    repo_id: str,
    scope: Scope,
    iid: int,
    use_max: bool,
    task_kwargs: dict[str, Any],
    trigger_type: SessionOrigin,
    title: str,
    ref: str = "",
) -> bool:
    """Enqueue ``task`` with ``task_kwargs`` plus what every addressor task takes, and record its ``Run`` on the
    issue's or merge request's ``Session`` in one transaction. Returns whether it did: ``False`` when the enqueue
    found an earlier trigger's task, which has its ``Run`` already.

    A failed session or ``Run`` write rolls the enqueue back and raises, so the platform redelivers the event.
    """
    thread_id = compute_thread_id(repo_slug=repo_id, scope=scope, entity_iid=iid)
    # user=None: the webhook fires for whoever touched the issue/MR, not the agent owner, so USER envs never apply.
    sandbox_env = await resolve_env_for_run(user=None, repo_id=repo_id)
    sandbox_environment_id = str(sandbox_env.id) if sandbox_env is not None else None
    agent_model, agent_thinking_level = _pinned_model(use_max)
    user = await resolve_user(author.provider, author.uid, username=author.username, email=author.email)

    @sync_to_async
    @transaction.atomic
    def enqueue_with_run() -> bool:
        result, created = task.get_or_enqueue(
            repo_id=repo_id,
            thread_id=thread_id,
            sandbox_environment_id=sandbox_environment_id,
            use_max=use_max,
            **task_kwargs,
        )
        if created:
            create_run(
                trigger_type=trigger_type,
                task_result_id=result.id,
                thread_id=thread_id,
                repo_id=repo_id,
                ref=ref,
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                merge_request_iid=iid if scope == Scope.MERGE_REQUEST else None,
                user=user,
                external_username=author.username,
                title=title,
                sandbox_environment_id=sandbox_environment_id,
            )
        return created

    return await enqueue_with_run()
