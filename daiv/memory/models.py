from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class ObservationCategory(models.TextChoices):
    BUILD_TEST = "build_test", _("Build & test")
    CODEBASE_FACT = "codebase_fact", _("Codebase fact")
    PITFALL = "pitfall", _("Pitfall")
    REVIEWER_PREFERENCE = "reviewer_preference", _("Reviewer preference")
    WORKFLOW = "workflow", _("Workflow")


class ObservationStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    CONSOLIDATED = "consolidated", _("Consolidated")
    DISCARDED = "discarded", _("Discarded")


class MemoryObservation(models.Model):
    """A candidate learning extracted from a single finished agent run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repo_id = models.CharField(_("repository"), max_length=255, db_index=True)
    run = models.ForeignKey(
        "agent_sessions.Run",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_observations",
        verbose_name=_("run"),
    )
    category = models.CharField(_("category"), max_length=32, choices=ObservationCategory.choices)
    content = models.TextField(_("content"))
    status = models.CharField(
        _("status"), max_length=16, choices=ObservationStatus.choices, default=ObservationStatus.PENDING
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Memory observation")
        verbose_name_plural = _("Memory observations")
        ordering = ["created_at"]
        indexes = [models.Index(fields=["repo_id", "status"], name="memory_obs_repo_status_idx")]

    def __str__(self) -> str:
        return f"{self.repo_id}: [{self.get_category_display()}] {self.content[:50]}"


class RepositoryMemory(models.Model):
    """The consolidated, bounded memory document for a repository. One row per repo."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repo_id = models.CharField(_("repository"), max_length=255, unique=True)
    content = models.TextField(_("content"), blank=True, default="")
    last_consolidated_at = models.DateTimeField(_("last consolidated at"), null=True, blank=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Repository memory")
        verbose_name_plural = _("Repository memories")

    def __str__(self) -> str:
        return f"Memory for {self.repo_id}"
