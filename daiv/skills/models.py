from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel


class GlobalSkill(TimeStampedModel):
    """Audit metadata for an admin-uploaded global skill.

    The filesystem at ``CUSTOM_SKILLS_PATH`` is the source of truth that the
    agent's ``SkillsMiddleware`` reads. This row exists to power the listing
    UI and attribute the upload.
    """

    name = models.SlugField(_("name"), max_length=80, unique=True)
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
