"""Start an addressor run on an issue or a merge request, in its own session.

Each start enqueues the addressor task and records its ``Run`` on the issue's or merge request's ``Session`` in one
transaction. A worker sees the task only once its session and ``Run`` exist, so every addressor run waits for the
session's slot and knows its ``Run``. The task's ``dedup_on`` key names the trigger: a repeated delivery or an edited
comment finds the earlier trigger's task, which has its ``Run`` already, and stops without writing. The enqueue holds
the key's lock until the transaction commits, so of concurrent deliveries exactly one creates the task and its
``Run``. A trigger whose session is archived starts nothing; a mention is answered once, linking the session, for
someone with run access to unarchive it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.cache import cache
from django.db import transaction
from django.tasks.base import TaskResultStatus
from django.urls import reverse

from asgiref.sync import sync_to_async
from django_tasks_db.models import DBTaskResult
from sandbox_envs.selection import resolve_env_for_run

from codebase.base import GitPlatform, Scope
from codebase.clients.base import PLATFORM_ERRORS
from core.site_settings import site_settings
from core.utils import build_absolute_url
from sessions.ids import compute_thread_id
from sessions.models import Session
from sessions.services import create_run

if TYPE_CHECKING:
    from collections.abc import Callable

    from accounts.models import User
    from codebase.base import DiffLocation
    from core.backends.deduplicating import DeduplicatingTask
    from sessions.models import Run, SessionOrigin

logger = logging.getLogger("daiv.sessions")

ARCHIVED_REPLY_TTL = 24 * 60 * 60
MENTION_TASK_PATH_BEFORE_THE_MOVE = "webhooks.tasks.address_mr_comments_task"


@dataclass(frozen=True, kw_only=True)
class Caller:
    """Who asks for a run: a mention's author, a job's submitter or the account that triggered a CI pipeline.

    ``user`` owns the run, and is notified about it: the submitter, or the DAIV user linked to the platform account
    (``None`` when no one linked it). ``provider``, ``uid`` and ``username`` name that platform account, blank for a
    submitter who never linked one; DAIV mentions ``username`` on the merge request. The run acts as ``user``, loading
    their personal tools and environments, only with ``acts_as_user``.
    """

    user: User | None
    provider: GitPlatform | str = ""
    uid: int | str = ""
    username: str = ""
    acts_as_user: bool = True

    @property
    def acting_user(self) -> User | None:
        """The DAIV user the run acts as: ``user``, unless it is only attributed to them."""
        return self.user if self.acts_as_user else None

    @property
    def run_fields(self) -> dict[str, Any]:
        """The ``Run`` fields that name this caller."""
        return {
            "user": self.user,
            "external_username": self.username,
            "external_provider": self.provider,
            "external_uid": str(self.uid),
            "acts_as_user": self.acts_as_user,
        }

    @property
    def author_fields(self) -> dict[str, Any]:
        """The ``InboxEvent`` fields that name this caller as an event's author."""
        return {
            "author": self.user,
            "author_username": self.username,
            "author_provider": self.provider,
            "author_uid": str(self.uid),
        }


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

    location: DiffLocation | None = None
    """Where on the diff a comment on it was left."""


@dataclass(frozen=True, kw_only=True)
class Request:
    """A job for a merge request, from the jobs API or MCP (``trigger_type``). ``request_key`` names the job, a fresh
    ``SourceKey.job`` each, so no two jobs are one.

    ``references`` are stored refs (``RefIn.to_stored``). ``mcp_switches`` are the caller's MCP switches, which an
    owning session's run applies to its own selection, and ``mcp_overrides`` the selection admission resolved them to,
    which the merge request's own session runs with. ``sandbox_environment_id`` is the environment admission resolved,
    which the merge request's own session runs in; ``environment_named``: the caller named it, so it also replaces an
    owning session's."""

    prompt: str
    trigger_type: SessionOrigin
    request_key: str
    muted: bool | None = None
    agent_model: str = ""
    agent_thinking_level: str = ""
    references: tuple[dict[str, Any], ...] = ()
    mcp_switches: dict[str, bool] | None = None
    mcp_overrides: dict[str, str] | None = None
    sandbox_environment_id: str | None = None
    environment_named: bool = False

    @property
    def named_environment_id(self) -> str | None:
        return self.sandbox_environment_id if self.environment_named else None


@dataclass(frozen=True)
class StartOutcome:
    """What a start did, in the session ``thread_id``.

    ``started``: it enqueued a run or posted an event, rather than finding an earlier trigger's. ``run`` is the run a
    job reports, ``None`` when a mention joined a run that doesn't exist yet. ``archived``: the session is archived, so
    nothing started.
    """

    thread_id: str
    started: bool = False
    run: Run | None = None
    archived: bool = False


class RunStarter:
    """Starts addressor runs on one issue or merge request, in its session (:func:`sessions.ids.compute_thread_id`)."""

    def __init__(self, *, repo_id: str, scope: Scope, iid: int):
        self.repo_id = repo_id
        self.scope = scope
        self.iid = iid
        self.thread_id = compute_thread_id(repo_slug=repo_id, scope=scope, entity_iid=iid)

    async def astart(
        self,
        task: DeduplicatingTask,
        caller: Caller,
        *,
        use_max: bool,
        task_kwargs: dict[str, Any],
        trigger_type: SessionOrigin,
        title: str,
        reply: Callable[[str], object] | None,
        ref: str = "",
        request: Request | None = None,
        merge_request_web_url: str = "",
    ) -> StartOutcome:
        """Enqueue ``task`` with ``task_kwargs`` plus what every addressor task takes, and record its ``Run`` for
        ``caller`` in one transaction; a job's ``request`` gives its run's prompt, options and ``runs_alone``.
        Nothing starts when the enqueue finds an earlier trigger's task, which has its ``Run`` already, or when the
        session is archived, which ``reply`` answers; with no ``reply`` nothing is posted.

        The run uses the job's model when it names one, else the model ``use_max`` pins, and the job's environment
        when it has one, else the repository's environment for no one in particular.

        A failed session or ``Run`` write rolls the enqueue back and raises, so the platform redelivers the event.
        """
        if await Session.objects.filter(pk=self.thread_id).archived().aexists():
            if reply is None:
                logger.info("Ignoring a request on session %s: it is archived", self.thread_id)
            else:
                await self.areply_archived(reply, caller, self.thread_id, note_id=task_kwargs.get("mention_note_id"))
            return StartOutcome(thread_id=self.thread_id, archived=True)
        if request is not None:
            sandbox_environment_id = request.sandbox_environment_id
        else:
            # user=None: a webhook fires for whoever touched the issue/MR, not the agent owner, so no USER env applies.
            sandbox_env = await resolve_env_for_run(user=None, repo_id=self.repo_id)
            sandbox_environment_id = str(sandbox_env.id) if sandbox_env is not None else None
        agent_model, agent_thinking_level = (request.agent_model, request.agent_thinking_level) if request else ("", "")
        if not agent_model:
            agent_model, agent_thinking_level = self.pinned_model(use_max)

        @sync_to_async
        @transaction.atomic
        def enqueue_with_run() -> StartOutcome:
            if self._queued_before_the_move(
                task_kwargs.get("mention_note_id"), on_diff=task_kwargs.get("mention_on_diff", False)
            ):
                return StartOutcome(thread_id=self.thread_id)
            result, created = task.get_or_enqueue(
                repo_id=self.repo_id,
                thread_id=self.thread_id,
                sandbox_environment_id=sandbox_environment_id,
                use_max=use_max,
                **task_kwargs,
            )
            if not created:
                return StartOutcome(thread_id=self.thread_id)
            run = create_run(
                trigger_type=trigger_type,
                task_result_id=result.id,
                thread_id=self.thread_id,
                repo_id=self.repo_id,
                ref=ref,
                prompt=request.prompt if request is not None else "",
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                merge_request_iid=self.iid if self.scope == Scope.MERGE_REQUEST else None,
                merge_request_web_url=merge_request_web_url,
                **caller.run_fields,
                runs_alone=request is not None,
                muted=request.muted if request is not None else None,
                title=title,
                sandbox_environment_id=sandbox_environment_id,
            )
            return StartOutcome(thread_id=self.thread_id, started=True, run=run)

        return await enqueue_with_run()

    def _queued_before_the_move(self, note_id: int | None, *, on_diff: bool) -> bool:
        """Whether the merge request mention ``note_id`` has a task queued, running or done under the path and key it
        had before it moved to this app, which the new task's dedup can't see; a diff mention never ran there. SYNC
        ONLY. For one release."""
        if self.scope != Scope.MERGE_REQUEST or note_id is None or on_diff:
            return False
        return DBTaskResult.objects.filter(
            task_path=MENTION_TASK_PATH_BEFORE_THE_MOVE,
            status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING, TaskResultStatus.SUCCESSFUL],
            args_kwargs__kwargs__repo_id=self.repo_id,
            args_kwargs__kwargs__merge_request_id=self.iid,
            args_kwargs__kwargs__mention_note_id=note_id,
        ).exists()

    @staticmethod
    def pinned_model(use_max: bool) -> tuple[str, str]:
        """The agent model and thinking level to record: the site's max ones for ``daiv-max``, else none."""
        if use_max:
            return site_settings.agent_max_model_name, site_settings.agent_max_thinking_level
        return "", ""

    @staticmethod
    def session_url(thread_id: str) -> str:
        """The absolute address of the session ``thread_id``'s page. SYNC ONLY."""
        return build_absolute_url(reverse("session_detail", kwargs={"thread_id": thread_id}))

    @staticmethod
    async def areply_archived(
        reply: Callable[[str], object], caller: Caller, thread_id: str, *, note_id: int | None
    ) -> None:
        """Tell ``caller`` the session ``thread_id`` is archived, once per trigger, so a redelivery posts nothing; a
        label trigger has no ``note_id`` and is told once a day."""
        logger.info("Ignoring a trigger on session %s: it is archived", thread_id)
        key = f"webhooks:archived-reply:{thread_id}:{note_id or 'label'}"
        if not await cache.aadd(key, True, ARCHIVED_REPLY_TTL):
            return

        @sync_to_async
        def post() -> None:
            url = RunStarter.session_url(thread_id)
            reply(
                f"@{caller.username} This session is archived, so I won't act on this. Anyone who can run DAIV on "
                f"this repository can unarchive it: {url}"
            )

        try:
            await post()
        except PLATFORM_ERRORS:
            await cache.adelete(key)
            logger.exception("Failed to tell %s that session %s is archived", caller.username, thread_id)
