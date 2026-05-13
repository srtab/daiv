from __future__ import annotations

import logging

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from automation.titling.services import TitlerService
from automation.titling.tasks import generate_title_task
from chat.managers import ChatThreadManager

logger = logging.getLogger("daiv.chat")


class ChatThread(models.Model):
    """Metadata row for a chat conversation. The ``thread_id`` is the LangGraph
    checkpoint key — shared with any ``activity.Activity`` that produced the run we're
    continuing.
    """

    thread_id = models.CharField(max_length=64, primary_key=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_threads")
    repo_id = models.CharField(_("repository"), max_length=255)
    ref = models.CharField(_("ref"), max_length=255, blank=True, default="")
    title = models.CharField(max_length=120, blank=True, default="")
    # NULL means "free slot"; any non-NULL value is the run_id currently holding
    # the thread. Empty string is forbidden by ``chat_active_run_id_nonempty``
    # so the sentinel is unambiguous.
    active_run_id = models.CharField(max_length=64, null=True, blank=True, default=None)  # noqa: DJ001
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(auto_now=True)

    objects = ChatThreadManager()

    class Meta:
        ordering = ["-last_active_at"]
        indexes = [models.Index(fields=["user", "-last_active_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(active_run_id__isnull=True) | ~models.Q(active_run_id=""),
                name="chat_active_run_id_nonempty",
            )
        ]

    def __str__(self) -> str:
        return str(self.title or self.thread_id)

    @classmethod
    async def aget_or_create_from_activity(cls, user, activity) -> tuple[ChatThread, bool]:
        """Look up or create a thread that continues an activity run. Idempotent."""
        # Reuse the activity's already-generated title when present — both rows describe
        # the same underlying run, so re-titling would just spend tokens to land on the
        # same answer.
        existing_title = (activity.title or "").strip()
        thread, created = await cls.objects.aget_or_create(
            thread_id=activity.thread_id,
            defaults={
                "user": user,
                "repo_id": activity.repo_id,
                "ref": activity.ref or "",
                "title": existing_title or TitlerService.heuristic(activity.prompt or ""),
            },
        )
        if created and not existing_title and activity.prompt:
            try:
                await generate_title_task.aenqueue(
                    entity_type="chat_thread",
                    pk=thread.thread_id,
                    prompt=activity.prompt,
                    repo_id=activity.repo_id,
                    ref=activity.ref or "",
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to enqueue title task for chat thread %s", thread.thread_id)
        return thread, created
