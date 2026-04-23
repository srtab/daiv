from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from chat.managers import ChatThreadManager


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
    active_run_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(auto_now=True)

    objects = ChatThreadManager()

    class Meta:
        ordering = ["-last_active_at"]
        indexes = [models.Index(fields=["user", "-last_active_at"])]

    def __str__(self) -> str:
        return self.title or self.thread_id

    @classmethod
    async def aget_or_create_from_activity(cls, user, activity) -> tuple[ChatThread, bool]:
        """Look up or create a thread that continues an activity run. Idempotent."""
        existing = await cls.objects.filter(thread_id=activity.thread_id).afirst()
        if existing is not None:
            return existing, False
        thread = await cls.objects.acreate(
            user=user,
            thread_id=activity.thread_id,
            repo_id=activity.repo_id,
            ref=activity.ref or "",
            title=(activity.prompt or "")[:120],
        )
        return thread, True
