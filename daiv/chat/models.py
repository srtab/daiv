from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from automation.titling.services import TitlerService
from automation.titling.tasks import generate_title_task
from chat.managers import ChatThreadManager
from core.models import TokenUsageRecord

if TYPE_CHECKING:
    from automation.agent.usage_tracking import UsageSummary

logger = logging.getLogger("daiv.chat")


class ChatThread(TokenUsageRecord):
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

    # Override mixin fields with wider types appropriate for cumulative storage.
    input_tokens = models.PositiveBigIntegerField(_("input tokens"), null=True, blank=True)
    output_tokens = models.PositiveBigIntegerField(_("output tokens"), null=True, blank=True)
    total_tokens = models.PositiveBigIntegerField(_("total tokens"), null=True, blank=True)
    cost_usd = models.DecimalField(_("cost (USD)"), max_digits=12, decimal_places=6, null=True, blank=True)

    # Chat-only bookkeeping
    cache_read_tokens = models.PositiveBigIntegerField(_("cache read tokens"), default=0)
    cache_write_tokens = models.PositiveBigIntegerField(_("cache write tokens"), default=0)
    last_input_tokens = models.PositiveIntegerField(_("last input tokens"), default=0)
    last_model_name = models.CharField(_("last model name"), max_length=128, blank=True, default="")
    cost_priced = models.BooleanField(_("cost priced"), default=True)

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

    def apply_usage_delta(
        self, summary: UsageSummary, last_model_name: str | None, last_input_tokens: int
    ) -> list[str]:
        """Fold a per-run UsageSummary into this thread's cumulative totals.

        Returns the list of field names that changed (suitable for ``save(update_fields=...)``).
        Does **not** save. Returns ``[]`` for an empty summary.
        """
        from chat.usage import cache_token_totals

        if summary.total_tokens == 0:
            return []

        changed: list[str] = []

        self.input_tokens = (self.input_tokens or 0) + summary.input_tokens
        self.output_tokens = (self.output_tokens or 0) + summary.output_tokens
        self.total_tokens = (self.total_tokens or 0) + summary.total_tokens
        changed.extend(["input_tokens", "output_tokens", "total_tokens"])

        cr, cw = cache_token_totals(summary.by_model or {})
        if cr:
            self.cache_read_tokens += cr
            changed.append("cache_read_tokens")
        if cw:
            self.cache_write_tokens += cw
            changed.append("cache_write_tokens")

        if last_model_name:
            self.last_model_name = last_model_name
            changed.append("last_model_name")
        if last_input_tokens:
            self.last_input_tokens = last_input_tokens
            changed.append("last_input_tokens")

        # Sticky-null cost: once any unpriced delta lands, the cumulative cost is
        # unknown for the rest of the thread's lifetime. Re-priced deltas can't
        # restore confidence in earlier missing data.
        if summary.cost_usd is None or not self.cost_priced:
            if self.cost_priced:
                self.cost_priced = False
                changed.append("cost_priced")
            if self.cost_usd is not None:
                self.cost_usd = None
                changed.append("cost_usd")
        else:
            try:
                delta_cost = Decimal(summary.cost_usd)
            except InvalidOperation, TypeError, ValueError:
                logger.error(
                    "Invalid cost_usd %r from UsageSummary for ChatThread(thread_id=%r) "
                    "model=%r total_tokens=%d; degrading thread to unpriced",
                    summary.cost_usd,
                    self.thread_id,
                    last_model_name,
                    summary.total_tokens,
                )
                if self.cost_priced:
                    self.cost_priced = False
                    changed.append("cost_priced")
                if self.cost_usd is not None:
                    self.cost_usd = None
                    changed.append("cost_usd")
            else:
                self.cost_usd = (self.cost_usd or Decimal("0")) + delta_cost
                changed.append("cost_usd")

        if summary.by_model:
            merged = dict(self.usage_by_model or {})
            for model, entry in summary.by_model.items():
                existing = dict(merged.get(model) or {})
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    existing[key] = int(existing.get(key) or 0) + int(entry.get(key) or 0)
                for detail_key in ("input_token_details", "output_token_details"):
                    if detail_key in entry:
                        existing[detail_key] = dict(entry[detail_key])
                new_cost = entry.get("cost_usd")
                if "cost_usd" not in existing:
                    existing["cost_usd"] = new_cost
                elif existing["cost_usd"] is None or new_cost is None:
                    # Sticky null per model — same rationale as the cumulative case above.
                    existing["cost_usd"] = None
                else:
                    try:
                        existing["cost_usd"] = str(Decimal(existing["cost_usd"]) + Decimal(new_cost))
                    except InvalidOperation, TypeError, ValueError:
                        logger.warning(
                            "Invalid per-model cost for ChatThread(thread_id=%r) model=%r "
                            "old=%r new=%r; sticky-null this model's cost",
                            self.thread_id,
                            model,
                            existing["cost_usd"],
                            new_cost,
                        )
                        existing["cost_usd"] = None
                merged[model] = existing
            self.usage_by_model = merged
            changed.append("usage_by_model")

        return changed
