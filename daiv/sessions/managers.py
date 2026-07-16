from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.models import Run, RunEnvelope, Session


class SessionQuerySet(models.QuerySet["Session"]):
    def _owner_q(self, user: User) -> models.Q:
        """Ownership predicate shared by ``by_owner``/``visible_to``.

        A user owns a session when they own the session row, match its
        ``external_username``, subscribe to its schedule, or acted in any of its runs
        (user FK or external_username on the Run). The run-level match preserves
        per-actor visibility on shared webhook threads.
        """
        from sessions.models import Run

        run_match = Run.objects.filter(session=models.OuterRef("pk")).filter(
            models.Q(user=user) | models.Q(external_username=user.username)
        )
        return (
            models.Q(user=user)
            | models.Q(external_username=user.username)
            | models.Q(scheduled_job__subscribers=user)
            | models.Exists(run_match)
        )

    def by_owner(self, user: User) -> models.QuerySet[Session]:
        """Sessions the user owns (ownership only).

        This is the visibility boundary for thread continuation / API lookups. It builds
        with no platform-identity DB read, so it is safe to construct inside async query
        chains (``await ...by_owner(user).aexists()``). Use :meth:`visible_to` for the
        broader "can view" surface that also includes repo-read access.
        """
        if user.is_admin:
            return self.all()
        return self.filter(self._owner_q(user)).distinct()

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

        return self.filter(self._owner_q(user) | models.Q(repo_id__in=viewable_repo_ids_subquery(user))).distinct()

    def with_latest_status(self) -> models.QuerySet[Session]:
        """Annotate each session with ``latest_run_status`` (status of the newest run).

        NULL for chat-only sessions that predate run tracking.
        """
        from sessions.models import Run

        latest = Run.objects.filter(session=models.OuterRef("pk")).order_by("-created_at", "-id")
        return self.annotate(latest_run_status=models.Subquery(latest.values("status")[:1]))


# ``by_owner``/``visible_to``/``with_latest_status`` live on the QuerySet so they chain
# (``Session.objects.visible_to(user).with_latest_status()``); the manager re-exports
# them for the bare ``Session.objects.by_owner(...)`` call sites.
class SessionManager(models.Manager.from_queryset(SessionQuerySet)):
    pass


class RunManager(models.Manager["Run"]):
    def _owner_q(self, user: User) -> models.Q:
        return (
            models.Q(user=user)
            | models.Q(external_username=user.username)
            | models.Q(session__scheduled_job__subscribers=user)
        )

    def by_owner(self, user: User) -> models.QuerySet[Run]:
        """Runs the user owns (ownership only). Async-safe; see :meth:`SessionQuerySet.by_owner`."""
        if user.is_admin:
            return self.all()
        return self.filter(self._owner_q(user)).distinct()

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

        return self.filter(self._owner_q(user) | models.Q(repo_id__in=viewable_repo_ids_subquery(user))).distinct()

    def by_batch(self, batch_id) -> models.QuerySet[Run]:
        return self.filter(batch_id=batch_id)


class RunEnvelopeManager(models.Manager["RunEnvelope"]):
    def for_run(self, run: Run) -> RunEnvelope | None:
        """Return the run's classification envelope, or ``None`` while it is still pending.

        The None-safe read accessor for a run's envelope. ``None`` is the "classifying…"
        state: a just-finished run may not have its envelope written yet (the Epic 2 Feed
        renders that pending state). Prefer this over the ``run.envelope`` reverse OneToOne,
        which raises ``RunEnvelope.DoesNotExist`` for a pending run rather than returning
        ``None``.
        """
        return self.filter(run=run).first()
