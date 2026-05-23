from __future__ import annotations

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from skills.constants import SKILL_NAME_RE


class GlobalSkill(TimeStampedModel):
    """Audit metadata for an admin-uploaded global skill.

    The filesystem at ``CUSTOM_SKILLS_PATH`` is the source of truth that the
    agent's ``SkillsMiddleware`` reads. This row exists to power the listing
    UI and attribute the upload.
    """

    name = models.SlugField(_("name"), max_length=80, unique=True, validators=[RegexValidator(regex=SKILL_NAME_RE)])
    description = models.CharField(_("description"), max_length=1024, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="uploaded_skills"
    )
    size_bytes = models.PositiveIntegerField(_("size (bytes)"))
    file_count = models.PositiveIntegerField(_("file count"))
    checksum = models.CharField(_("checksum"), max_length=64)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SkillInvocation(TimeStampedModel):
    """One row per skill invocation by the agent.

    Persisted by ``SkillsMiddleware._skill_tool_generator`` after a skill is
    resolved and its body downloaded — i.e. when the agent commits to running
    the skill. Powers the admin counts and 30-day chart on the Skills pages.
    """

    class Source(models.TextChoices):
        BUILTIN = "builtin", _("Built-in")
        GLOBAL = "global", _("Custom global")
        REPO = "repo", _("Per-repo")

    name = models.SlugField(_("name"), max_length=80, validators=[RegexValidator(regex=SKILL_NAME_RE)])
    source = models.CharField(_("source"), max_length=16, choices=Source.choices)
    repo_slug = models.CharField(_("repository"), max_length=255)
    thread_id = models.UUIDField(_("thread id"))

    class Meta:
        # (name, source, -created) covers every read path: the list-view
        # GROUP BY (name, source) for counts and the detail-view filter on
        # (name, source) + TruncDate(created) for the 30-day chart.
        indexes = [models.Index(fields=["name", "source", "-created"])]
        constraints = [models.CheckConstraint(condition=Q(repo_slug__gt=""), name="skillinvocation_repo_slug_nonempty")]

    def __str__(self) -> str:
        return f"{self.name} ({self.source}) @ {self.created:%Y-%m-%d %H:%M}"
