"""Start an addressor run for a webhook event that a platform's callback has parsed and accepted.

An issue trigger starts a run in the issue's session (:class:`sessions.merge_request_runs.starter.RunStarter`), and a
comment on a merge request goes where :class:`sessions.merge_request_runs.service.MergeRequestRuns` sends it. DAIV
reacts with 👀 on what started a run or woke a session; a repeated delivery or an edited comment starts nothing and gets
no second reaction.
"""

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from sessions.merge_request_runs.service import MergeRequestRuns
from sessions.merge_request_runs.starter import Caller, Mention, RunStarter
from sessions.models import SessionOrigin

from accounts.utils import resolve_linked_user
from codebase.base import GitPlatform, Scope
from codebase.clients.base import PLATFORM_ERRORS, Emoji
from webhooks.tasks import address_issue_label_task, address_issue_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from codebase.clients import RepoClient

__all__ = ["Author", "Mention", "dispatch_issue_run", "dispatch_mr_comment", "dispatch_mr_comment_run"]

logger = logging.getLogger("daiv.webhooks")


@dataclass(frozen=True)
class Author:
    """The platform account that triggered the event."""

    provider: GitPlatform
    uid: int
    username: str

    async def acaller(self) -> Caller:
        """This account as a run's caller: the DAIV user who signed in with it
        (:func:`accounts.utils.resolve_linked_user`) owns, acts in and is notified about what it starts."""
        return Caller(
            user=await resolve_linked_user(self.provider, self.uid),
            provider=self.provider,
            uid=self.uid,
            username=self.username,
        )


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
    outcome = await RunStarter(repo_id=repo_id, scope=Scope.ISSUE, iid=issue_iid).astart(
        address_issue_task if mention else address_issue_label_task,
        await author.acaller(),
        use_max=use_max,
        task_kwargs={"issue_iid": issue_iid, "mention_comment_id": comment_id, "mention_note_id": note_id},
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        title=title,
        reply=lambda body: client.create_issue_comment(repo_id, issue_iid, body, reply_to_id=comment_id),
    )
    if outcome.started:
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
    """Address a comment that mentioned DAIV on a merge request, in the session that owns it when one does."""
    outcome = await MergeRequestRuns(client, repo_id, merge_request_iid).astart(
        await author.acaller(), mention, title=title, ref=ref, use_max=use_max
    )
    if outcome.started:
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


async def dispatch_mr_comment(
    *, client: RepoClient, repo_id: str, merge_request_iid: int, title: str, author: Author, comment: Mention
) -> None:
    """Remember a comment that did not mention DAIV in the session that owns the merge request; with no such session
    it concerns no one."""
    await MergeRequestRuns(client, repo_id, merge_request_iid).aremember(author.acaller, comment, title=title)


async def _react(add_reaction: Callable[[], object], target: str) -> None:
    try:
        await sync_to_async(add_reaction)()
    except PLATFORM_ERRORS:
        logger.exception("Failed to add reaction to %s", target)
