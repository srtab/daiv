from django.db import models
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel


class PlatformType(models.TextChoices):
    GITLAB = "gitlab", _("GitLab")
    GITHUB = "github", _("GitHub")


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
