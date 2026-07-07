from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from codebase.base import RepoAccessLevel
from codebase.conf import settings as codebase_settings


class PlatformType(models.TextChoices):
    GITLAB = "gitlab", _("GitLab")
    GITHUB = "github", _("GitHub")


# Access tiers have a single source of truth: ``RepoAccessLevel`` in ``codebase.base`` (used by
# the clients, the sync task and the authorization service). The DB field derives its choices
# from it so the two can never silently drift; a new tier without a label here fails loudly.
_ACCESS_LEVEL_LABELS = {RepoAccessLevel.READ: _("Read"), RepoAccessLevel.WRITE: _("Write")}
ACCESS_LEVEL_CHOICES = [(level.value, _ACCESS_LEVEL_LABELS[level]) for level in RepoAccessLevel]


class MergeMetric(TimeStampedModel):
    """
    Tracks merge metrics for MRs/PRs merged into default branches.

    Records lines added/removed, files changed, and split DAIV/human attribution
    based on per-commit author analysis. Used to measure DAIV's impact on codebases
    and track code velocity over time.

    Note: ``daiv_lines_added + human_lines_added`` may not equal ``lines_added`` because
    MR-level diff stats and per-commit stats come from different API endpoints that can
    disagree (e.g. after squash merges or rebases). When one API call fails, the split
    attribution fields may be zero while totals are populated, or vice versa.
    """

    repo_id = models.CharField(_("repository ID"), max_length=255, db_index=True)
    merge_request_iid = models.PositiveIntegerField(_("merge request IID"))
    title = models.CharField(_("title"), max_length=512, blank=True)
    lines_added = models.PositiveIntegerField(_("lines added"), default=0)
    lines_removed = models.PositiveIntegerField(_("lines removed"), default=0)
    files_changed = models.PositiveIntegerField(_("files changed"), default=0)
    daiv_lines_added = models.PositiveIntegerField(_("DAIV lines added"), default=0)
    daiv_lines_removed = models.PositiveIntegerField(_("DAIV lines removed"), default=0)
    human_lines_added = models.PositiveIntegerField(_("human lines added"), default=0)
    human_lines_removed = models.PositiveIntegerField(_("human lines removed"), default=0)
    total_commits = models.PositiveIntegerField(_("total commits"), default=0)
    daiv_commits = models.PositiveIntegerField(_("DAIV commits"), default=0)
    merged_at = models.DateTimeField(_("merged at"))
    target_branch = models.CharField(_("target branch"), max_length=255)
    source_branch = models.CharField(_("source branch"), max_length=255)
    platform = models.CharField(_("platform"), max_length=10, choices=PlatformType.choices)

    class Meta:
        verbose_name = _("Merge Metric")
        verbose_name_plural = _("Merge Metrics")
        unique_together = [("repo_id", "merge_request_iid", "platform")]
        indexes = [models.Index(fields=["repo_id", "merged_at"]), models.Index(fields=["merged_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(daiv_commits__lte=models.F("total_commits")), name="daiv_lte_total_commits"
            )
        ]

    def __str__(self) -> str:
        return f"{self.repo_id}!{self.merge_request_iid} ({self.daiv_commits}/{self.total_commits} DAIV)"

    def save(self, *args, **kwargs) -> None:
        if self.title and len(self.title) > 512:
            self.title = self.title[:512]
        super().save(*args, **kwargs)


def _hard_ttl_cutoff():
    """Cutoff for the hard-TTL freshness window shared by RepositoryAccess and RepositoryCatalog:
    a synced row is trusted only while ``synced_at >= _hard_ttl_cutoff()``."""
    return timezone.now() - timedelta(hours=codebase_settings.REPO_ACCESS_HARD_TTL_HOURS)


class RepositoryAccessQuerySet(models.QuerySet):
    """Freshness helpers so the hard-TTL invariant lives on the model, not in each caller.

    Authorization trusts a row only while it is ``fresh()``; the sync task prunes ``stale()``
    rows. Both derive the cutoff from the single ``REPO_ACCESS_HARD_TTL_HOURS`` setting, so no
    consumer can accidentally omit or diverge on the freshness window.
    """

    @staticmethod
    def _cutoff():
        return _hard_ttl_cutoff()

    def fresh(self):
        """Rows synced within the hard TTL — still trusted for authorization decisions."""
        return self.filter(synced_at__gte=self._cutoff())

    def stale(self):
        """Rows aged past the hard TTL — they grant no access and are pruned by the sync task."""
        return self.filter(synced_at__lt=self._cutoff())


class RepositoryAccess(models.Model):
    """
    Mirror of a platform user's effective access to a repository.

    Populated by the periodic sync task (one member-list call per bot-visible repo).
    Keyed by platform identity — rows exist for every member of every bot-visible repo,
    including platform users with no DAIV account, so a first OAuth login needs no sync.
    Absence of a row means no access — as does a row older than the hard TTL, which
    authorization treats as expired (see ``RepositoryAccessQuerySet.fresh``).
    """

    provider = models.CharField(_("provider"), max_length=10, choices=PlatformType.choices)
    uid = models.CharField(_("platform user ID"), max_length=191)
    username = models.CharField(_("platform username"), max_length=255, blank=True)
    repo_id = models.CharField(_("repository ID"), max_length=255)
    access_level = models.CharField(_("access level"), max_length=10, choices=ACCESS_LEVEL_CHOICES)
    synced_at = models.DateTimeField(_("synced at"))

    objects = RepositoryAccessQuerySet.as_manager()

    class Meta:
        verbose_name = _("Repository Access")
        verbose_name_plural = _("Repository Accesses")
        constraints = [models.UniqueConstraint(fields=["provider", "uid", "repo_id"], name="repo_access_unique")]
        indexes = [models.Index(fields=["provider", "uid"]), models.Index(fields=["provider", "repo_id"])]

    def __str__(self) -> str:
        return f"{self.provider}:{self.uid} -> {self.repo_id} ({self.access_level})"


class RepositoryAccessSyncState(models.Model):
    """Singleton bookkeeping row for the access sync's liveness and observability.

    Tracks scheduler liveness (``last_started_at``, which drives the backstop enqueue) and the
    last fully clean run (``last_success_at`` / ``status``, observability only). It does NOT gate
    access: the hard-TTL access decision is per-row (see ``RepositoryAccessQuerySet.fresh``).
    """

    SINGLETON_PK = 1

    class Status(models.TextChoices):
        NEVER = "never", _("Never synced")
        OK = "ok", _("OK")
        FAILED = "failed", _("Failed")

    last_started_at = models.DateTimeField(_("last started at"), null=True, blank=True)
    last_success_at = models.DateTimeField(_("last success at"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.NEVER)

    class Meta:
        verbose_name = _("Repository Access Sync State")
        verbose_name_plural = _("Repository Access Sync States")

    def __str__(self) -> str:
        return f"repository access sync: {self.status} (last success: {self.last_success_at})"


class RepositoryCatalogQuerySet(models.QuerySet):
    """Freshness helper mirroring RepositoryAccess: only rows synced within the hard TTL are
    trusted, so a dead sync eventually stops surfacing a stale catalog."""

    def fresh(self):
        return self.filter(synced_at__gte=_hard_ttl_cutoff())


class RepositoryCatalog(models.Model):
    """Local mirror of the bot-visible repository catalog.

    Upserted by the access sync (which already lists the universe each cycle). Repository
    *listings* (pickers, search, MCP) are served from here as local DB joins instead of live
    platform fetches. Not security-load-bearing: what a user may *view* is still gated by
    ``RepositoryAccess``; this table only supplies repo metadata and the admin-visible universe.
    """

    provider = models.CharField(_("provider"), max_length=10, choices=PlatformType.choices)
    slug = models.CharField(_("repository slug"), max_length=255)
    name = models.CharField(_("name"), max_length=255, blank=True)
    default_branch = models.CharField(_("default branch"), max_length=255, blank=True)
    html_url = models.CharField(_("HTML URL"), max_length=512, blank=True)
    topics = models.JSONField(_("topics"), default=list, blank=True)
    synced_at = models.DateTimeField(_("synced at"))

    objects = RepositoryCatalogQuerySet.as_manager()

    class Meta:
        verbose_name = _("Repository Catalog")
        verbose_name_plural = _("Repository Catalog")
        # The unique constraint's backing index already serves the ``provider`` + ``slug`` lookups
        # (equality and the ``provider``-prefixed ``slug__in``/``icontains`` scans), so no separate
        # index is declared — unlike ``RepositoryAccess``, whose indexes cover different prefixes
        # than its unique key.
        constraints = [models.UniqueConstraint(fields=["provider", "slug"], name="repo_catalog_unique")]

    def __str__(self) -> str:
        return f"{self.provider}:{self.slug}"
