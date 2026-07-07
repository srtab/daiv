from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.models import Run, Session


class SessionManager(models.Manager["Session"]):
    def by_owner(self, user: User) -> models.QuerySet[Session]:
        """Return sessions visible to the given user.

        Admins see all. Regular users see sessions where they own the session row,
        match its ``external_username``, subscribe to its schedule, or acted in any
        of its runs (user FK or external_username on the Run). The run-level match
        preserves per-actor visibility on shared webhook threads.
        """
        from sessions.models import Run

        if user.is_admin:
            return self.all()
        run_match = Run.objects.filter(session=models.OuterRef("pk")).filter(
            models.Q(user=user) | models.Q(external_username=user.username)
        )
        return self.filter(
            models.Q(user=user)
            | models.Q(external_username=user.username)
            | models.Q(scheduled_job__subscribers=user)
            | models.Exists(run_match)
        ).distinct()

    def with_latest_status(self) -> models.QuerySet[Session]:
        """Annotate each session with ``latest_run_status`` (status of the newest run).

        NULL for chat-only sessions that predate run tracking.
        """
        from sessions.models import Run

        latest = Run.objects.filter(session=models.OuterRef("pk")).order_by("-created_at", "-id")
        return self.annotate(latest_run_status=models.Subquery(latest.values("status")[:1]))


class RunManager(models.Manager["Run"]):
    def by_owner(self, user: User) -> models.QuerySet[Run]:
        """Mirror of the old ActivityManager.by_owner semantics, run-level."""
        if user.is_admin:
            return self.all()
        return self.filter(
            models.Q(user=user)
            | models.Q(external_username=user.username)
            | models.Q(session__scheduled_job__subscribers=user)
        ).distinct()

    def by_batch(self, batch_id) -> models.QuerySet[Run]:
        return self.filter(batch_id=batch_id)
