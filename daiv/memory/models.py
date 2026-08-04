from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from datetime import datetime


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


class MemoryObservationQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=ObservationStatus.PENDING)

    def unreplayed(self):
        """Consolidated observations that no entry links back to — what a backfill must replay."""
        return self.filter(status=ObservationStatus.CONSOLIDATED, entries__isnull=True)


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

    objects = MemoryObservationQuerySet.as_manager()

    class Meta:
        verbose_name = _("Memory observation")
        verbose_name_plural = _("Memory observations")
        ordering = ["created_at"]
        indexes = [models.Index(fields=["repo_id", "status"], name="memory_obs_repo_status_idx")]

    def __str__(self) -> str:
        return f"{self.repo_id}: [{self.get_category_display()}] {self.content[:50]}"


class EntryStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    SUPERSEDED = "superseded", _("Superseded")


class MemoryEntryQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status=EntryStatus.ACTIVE)


class MemoryEntry(models.Model):
    """A single durable fact in a repository's memory — the source of truth for the document.

    Entries are append-only in content: a change creates a successor (or none, for eviction) and
    marks the replaced row ``SUPERSEDED`` rather than rewriting it, so the history of every fact
    stays queryable. ``last_confirmed_at`` and ``observations`` are the exceptions, updated in
    place by :meth:`confirm`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repo_id = models.CharField(_("repository"), max_length=255, db_index=True)
    category = models.CharField(_("category"), max_length=32, choices=ObservationCategory.choices)
    content = models.TextField(_("content"))
    status = models.CharField(_("status"), max_length=16, choices=EntryStatus.choices, default=EntryStatus.ACTIVE)
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supersedes",
        verbose_name=_("superseded by"),
    )
    source_run = models.ForeignKey(
        "agent_sessions.Run",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_entries",
        verbose_name=_("source run"),
    )
    observations = models.ManyToManyField(
        MemoryObservation, blank=True, related_name="entries", verbose_name=_("observations")
    )
    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)
    last_confirmed_at = models.DateTimeField(_("last confirmed at"), default=timezone.now)

    objects = MemoryEntryQuerySet.as_manager()

    class Meta:
        verbose_name = _("Memory entry")
        verbose_name_plural = _("Memory entries")
        ordering = ["created_at"]
        indexes = [models.Index(fields=["repo_id", "status"], name="memory_entry_repo_status_idx")]

    def __str__(self) -> str:
        return f"{self.repo_id}: [{self.get_category_display()}] {self.content[:50]}"

    def supersede(self, successor: MemoryEntry | None = None) -> None:
        """Retire this entry, optionally naming its replacement.

        ``successor`` is ``None`` for budget eviction, where the entry is dropped from the
        document with nothing taking its place. ``content`` is deliberately never touched.
        """
        self.status = EntryStatus.SUPERSEDED
        self.superseded_by = successor
        self.save(update_fields=["status", "superseded_by"])

    def confirm(self, when: datetime) -> None:
        """Record that a fresh observation restated this entry's fact."""
        self.last_confirmed_at = when
        self.save(update_fields=["last_confirmed_at"])


class RepositoryMemory(models.Model):
    """The consolidated, bounded memory document for a repository. One row per repo.

    ``content`` is a deterministic render of the repository's active :class:`MemoryEntry` rows,
    kept here because it is the read location agent runs inject from.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repo_id = models.CharField(_("repository"), max_length=255, unique=True)
    content = models.TextField(_("content"), blank=True, default="")
    last_consolidated_at = models.DateTimeField(_("last consolidated at"), null=True, blank=True)
    # Bumped on every round, including the ones that change nothing, so a repository whose
    # consolidation keeps failing backs off like a healthy one instead of retrying hourly.
    last_attempted_at = models.DateTimeField(_("last attempted at"), null=True, blank=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Repository memory")
        verbose_name_plural = _("Repository memories")

    def __str__(self) -> str:
        return f"Memory for {self.repo_id}"
