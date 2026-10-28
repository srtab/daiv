from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from django.db import close_old_connections, models, transaction
from django.tasks import TaskResultStatus
from django.utils import timezone

from django_tasks_db.models import DBTaskResult

from core import ui_events
from core.constants import CANCELLED_BY_COORDINATOR_MESSAGE
from sessions.signal_defs import run_ended, run_finished

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.models import Run, RunArtifact, Session


_NEWEST_FIRST = ("-created_at", "-id")


def _started_by_q(user: User) -> models.Q:
    return models.Q(user=user) | models.Q(external_username=user.username)


def _session_runs_newest_first(session: models.OuterRef) -> models.QuerySet[Run]:
    from sessions.models import Run

    return Run.objects.filter(session=session).order_by(*_NEWEST_FIRST)


class SessionQuerySet(models.QuerySet["Session"]):
    def _owner_q(self, user: User) -> models.Q:
        """Ownership predicate shared by ``by_owner``/``visible_to``.

        A user owns a session when they own the session row, match its
        ``external_username``, subscribe to its schedule, or acted in any of its runs
        (user FK or external_username on the Run). The run-level match preserves
        per-actor visibility on shared webhook threads.
        """
        from sessions.models import Run

        run_match = Run.objects.started_by(user).filter(session=models.OuterRef("pk"))
        return (
            _started_by_q(user)
            | models.Q(scheduled_job__in=user.subscribed_schedules.values("pk"))
            | models.Exists(run_match)
        )

    def owned_by(self, user: User) -> models.QuerySet[Session]:
        """Sessions the user owns, admins included: the session list's "Mine" scope.

        Unlike :meth:`by_owner` this is a view filter, not a visibility boundary, so an admin's
        "Mine" is their own sessions rather than everything they may see.
        """
        return self.filter(self._owner_q(user))

    def by_owner(self, user: User) -> models.QuerySet[Session]:
        """Sessions the user owns (ownership only).

        This is the visibility boundary for thread continuation / API lookups. It builds
        with no platform-identity DB read, so it is safe to construct inside async query
        chains (``await ...by_owner(user).aexists()``). Use :meth:`visible_to` for the
        broader "can view" surface that also includes repo-read access.
        """
        if user.is_admin:
            return self.all()
        return self.owned_by(user)

    def visible_to(self, user: User) -> models.QuerySet[Session]:
        """Sessions the user may view: ownership OR a repository they can currently read.

        Adds sessions that ran on a repo with a fresh ``RepositoryAccess`` row to the
        ownership set. SYNC ONLY — resolving the caller's platform identity does a DB read
        at query-build time, so wrap in ``sync_to_async`` when used from an async view.
        """
        if user.is_admin:
            return self.all()
        # Local import: keep this module from pulling codebase.authorization
        # (and its codebase.* / allauth graph) in at app-load time.
        from codebase.authorization import viewable_repo_ids_subquery

        return self.filter(self._owner_q(user) | models.Q(repo_id__in=viewable_repo_ids_subquery(user)))

    def for_merge_request(self, iid: int) -> models.QuerySet[Session]:
        """Sessions that touched merge request ``iid``, whichever way they learned of it.

        MR-scope sessions carry the IID from the webhook; an issue-scope session only learns it
        when its run backfills at completion. A subquery rather than a ``runs`` join keeps this
        one row per session, so ``with_latest_status``'s per-row subquery is not multiplied.
        """
        from sessions.models import Run

        return self.filter(
            models.Q(merge_request_iid=iid)
            | models.Q(pk__in=Run.objects.filter(merge_request_iid=iid).values("session_id"))
        )

    def without_watch_placeholders(self) -> models.QuerySet[Session]:
        """Exclude the MR threads a CI watch created that no run has used yet."""
        from sessions.models import Run, SessionOrigin

        has_run = models.Exists(Run.objects.filter(session=models.OuterRef("pk")))
        return self.exclude(~has_run, origin=SessionOrigin.PIPELINE_WEBHOOK)

    def listed_for(self, user: User, *, mine: bool = False, archived: bool = False) -> models.QuerySet[Session]:
        """The rows the session list shows ``user`` before any filter: their own when ``mine``, else all visible.

        ``archived`` swaps in the sessions ``user`` archived, which every other list leaves out.
        """
        sessions = self.owned_by(user) if mine else self.visible_to(user)
        return sessions.archived_for(user, archived=archived).without_watch_placeholders().with_latest_status()

    def archived_for(self, user: User, *, archived: bool = True) -> models.QuerySet[Session]:
        """The sessions ``user`` archived out of their own list, or with ``archived=False`` the rest."""
        from sessions.models import HiddenSession

        hidden = models.Exists(HiddenSession.objects.filter(session=models.OuterRef("pk"), user=user))
        return self.filter(hidden if archived else ~hidden)

    def with_latest_status(self) -> models.QuerySet[Session]:
        """Annotate each session with ``latest_run_status`` (status of the newest run).

        NULL for chat-only sessions that predate run tracking.
        """
        latest = _session_runs_newest_first(models.OuterRef("pk"))
        return self.annotate(latest_run_status=models.Subquery(latest.values("status")[:1]))

    def with_row_runs(self) -> models.QuerySet[Session]:
        """Prefetch each session's runs, newest first, with only the columns a session row reads.

        Add a field here if ``sessions/_session_row.html`` starts reading it, or it becomes a deferred-field N+1.
        """
        from sessions.models import Run

        runs = Run.objects.only(
            "id",
            "session_id",
            "status",
            "started_at",
            "finished_at",
            "merge_request_iid",
            "merge_request_web_url",
            "cost_usd",
            "agent_model",
            "created_at",
        ).order_by(*_NEWEST_FIRST)
        return self.select_related("user", "scheduled_job").prefetch_related(models.Prefetch("runs", queryset=runs))


# ``by_owner``/``visible_to``/``with_latest_status`` live on the QuerySet so they chain
# (``Session.objects.visible_to(user).with_latest_status()``); the manager re-exports
# them for the bare ``Session.objects.by_owner(...)`` call sites.
class SessionManager(models.Manager.from_queryset(SessionQuerySet)):
    pass


class RunQuerySet(models.QuerySet["Run"]):
    def started_by(self, user: User) -> models.QuerySet[Run]:
        """Runs the user started, by account or by git platform username; not those of a schedule they subscribe to."""
        return self.filter(_started_by_q(user))

    def latest_in_session(self) -> models.QuerySet[Run]:
        """Each session's newest run, whoever started it: the run ``with_latest_status`` reads the status of."""
        newest = _session_runs_newest_first(models.OuterRef("session")).values("pk")[:1]
        return self.filter(pk=models.Subquery(newest))

    def with_retried(self, user: User) -> models.QuerySet[Run]:
        """Annotate ``retried``: a later run in another session took the run up again.

        That is a later run of the same schedule, or a later prompt-driven run ``user`` started on the same
        repository with the same prompt.
        """
        from sessions.models import SessionOrigin

        later = models.Q(created_at__gt=models.OuterRef("created_at")) & ~models.Q(session=models.OuterRef("session"))
        rerun = self.model.objects.filter(later, session__scheduled_job=models.OuterRef("session__scheduled_job"))
        retry = (
            self.model.objects
            .started_by(user)
            .filter(later, trigger_type__in=SessionOrigin.prompt_driven())
            .filter(repo_id=models.OuterRef("repo_id"), prompt=models.OuterRef("prompt"))
            .exclude(prompt="")
        )
        return self.annotate(retried=models.Exists(rerun) | models.Exists(retry))


class RunManager(models.Manager.from_queryset(RunQuerySet)):
    def _owner_q(self, user: User) -> models.Q:
        return _started_by_q(user) | models.Q(session__scheduled_job__in=user.subscribed_schedules.values("pk"))

    def visible_to(self, user: User) -> models.QuerySet[Run]:
        """Runs the user may view: ownership OR a repository they can currently read.

        SYNC ONLY (resolves platform identity via a DB read at build time); wrap in
        ``sync_to_async`` when used from an async view.
        """
        if user.is_admin:
            return self.all()
        # Local import: keep this module from pulling codebase.authorization
        # (and its codebase.* / allauth graph) in at app-load time.
        from codebase.authorization import viewable_repo_ids_subquery

        return self.filter(self._owner_q(user) | models.Q(repo_id__in=viewable_repo_ids_subquery(user)))

    def by_batch(self, batch_id) -> models.QuerySet[Run]:
        return self.filter(batch_id=batch_id)

    def finish(
        self,
        run: Run,
        status: str,
        *,
        where: models.Q | None = None,
        skip_dispatch: bool = False,
        never_started: bool = False,
        **fields: Any,
    ) -> bool:
        """Write the terminal ``status`` and ``fields`` onto ``run``'s row; the one writer of a run's end.

        The write is one guarded ``UPDATE``: it lands only while the row is not terminal yet and matches ``where``,
        so a finish never overwrites one that got there first. Returns whether this call finished the run. When it
        did, ``run`` takes the written values, the nav badge is refreshed once the write commits, ``run_ended`` goes
        out, and ``run_finished`` follows unless ``run`` is a chat run, which the classification, memory and dispatch
        receivers must not see.
        """
        _require_terminal(status)
        if not self._unfinished(run, where).update(status=status, **fields):
            return False
        self._apply(run, status, fields)
        transaction.on_commit(ui_events.publisher.runs_changed)
        self._emit_run_finished(run, skip_dispatch=skip_dispatch, never_started=never_started)
        return True

    async def afinish(
        self,
        run: Run,
        status: str,
        *,
        where: models.Q | None = None,
        skip_dispatch: bool = False,
        never_started: bool = False,
        **fields: Any,
    ) -> bool:
        """:meth:`finish` for async callers, from any event loop.

        The badge refresh and the receivers run in a worker thread: the receivers do synchronous ORM work, and the
        shared async Redis client is bound to the loop that first used it, and an ``async_to_sync`` caller runs on a
        fresh one.
        """
        _require_terminal(status)
        if not await self._unfinished(run, where).aupdate(status=status, **fields):
            return False
        self._apply(run, status, fields)
        await asyncio.to_thread(self._announce_finished, run, skip_dispatch=skip_dispatch, never_started=never_started)
        return True

    def cancel(self, run: Run) -> bool:
        """Ask an unfinished ``run`` to stop, and return whether this call did; ``run`` is left with its row's status.

        A run that is not RUNNING finishes FAILED here as ``never_started``, with ``CANCELLED_BY_COORDINATOR_MESSAGE``,
        and its task fails too unless a worker claimed it. A ``run_job_task`` running the run stops at its next
        :meth:`acancel_requested` check, before the agent if it has not reached it, and a RUNNING run finishes as that
        task does: with what it recorded, or SUCCESSFUL if it finished first.
        """
        from sessions.executor.run import RunStoppedError
        from sessions.models import RunStatus

        now = timezone.now()
        if not self._unfinished(run).filter(cancel_requested_at__isnull=True).update(cancel_requested_at=now):
            run.refresh_from_db(fields=["status", "cancel_requested_at"])
            return False
        run.cancel_requested_at = now
        finished = self.finish(
            run,
            RunStatus.FAILED,
            where=~models.Q(status=RunStatus.RUNNING),
            never_started=True,
            error_message=CANCELLED_BY_COORDINATOR_MESSAGE,
            finished_at=now,
            started_at=run.started_at or now,
        )
        if not finished:
            run.refresh_from_db(fields=["status"])
            return run.status not in RunStatus.terminal()
        DBTaskResult.objects.filter(run=run, status=TaskResultStatus.READY).update(
            status=TaskResultStatus.FAILED,
            finished_at=now,
            exception_class_path=f"{RunStoppedError.__module__}.{RunStoppedError.__qualname__}",
            traceback=CANCELLED_BY_COORDINATOR_MESSAGE,
        )
        return True

    async def acancel_requested(self, run_id: str) -> bool:
        """Whether :meth:`cancel` asked the run ``run_id`` to stop."""
        return await self.filter(pk=run_id, cancel_requested_at__isnull=False).aexists()

    def update_unfinished(self, run: Run, **fields: Any) -> bool:
        """Write a non-terminal change onto ``run``'s row unless the run has finished, so a stale sync never reopens it.

        Returns whether the write landed. When it moved the status, the nav badge is refreshed once it commits.
        """
        from sessions.models import RunStatus

        if fields.get("status") in RunStatus.terminal():
            raise ValueError(f"{fields['status']!r} is a terminal run status; finish the run instead")
        if not self._unfinished(run).update(**fields):
            return False
        if "status" in fields:
            transaction.on_commit(ui_events.publisher.runs_changed)
        return True

    def _announce_finished(self, run: Run, **flags: bool) -> None:
        try:
            ui_events.publisher.runs_changed()
            self._emit_run_finished(run, **flags)
        finally:
            # Runs in an executor thread no request lifecycle closes, so its pooled connection would be held forever.
            close_old_connections()

    def _unfinished(self, run: Run, where: models.Q | None = None) -> models.QuerySet[Run]:
        from sessions.models import RunStatus

        rows = self.filter(pk=run.pk).exclude(status__in=RunStatus.terminal())
        return rows if where is None else rows.filter(where)

    @staticmethod
    def _apply(run: Run, status: str, fields: dict[str, Any]) -> None:
        run.status = status
        for name, value in fields.items():
            setattr(run, name, value)

    @staticmethod
    def _emit_run_finished(run: Run, **flags: bool) -> None:
        from sessions.models import SessionOrigin

        run_ended.send_robust(sender=type(run), run=run)
        if run.trigger_type == SessionOrigin.CHAT:
            return
        run_finished.send_robust(sender=type(run), run=run, **flags)


def _require_terminal(status: str) -> None:
    from sessions.models import RunStatus

    if status not in RunStatus.terminal():
        raise ValueError(f"{status!r} is not a terminal run status")


class RunArtifactManager(models.Manager["RunArtifact"]):
    def visible_to(self, user: User) -> models.QuerySet[RunArtifact]:
        """Artifacts of the sessions the user may view, whose page lists every run's transcript. SYNC ONLY."""
        from sessions.models import Session

        if user.is_admin:
            return self.all()
        return self.filter(run__session__in=Session.objects.visible_to(user))
