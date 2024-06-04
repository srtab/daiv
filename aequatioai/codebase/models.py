from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import models

from django_extensions.db.models import TimeStampedModel

if TYPE_CHECKING:
    from codebase.base import Repository


class ClientChoices(models.TextChoices):
    GITLAB = "gitlab", "GitLab"
    GITHUB = "github", "GitHub"


class CodebaseNamespaceManager(models.Manager):
    def get_or_create_from_repository(self, repository: Repository) -> tuple[CodebaseNamespace, bool]:
        """
        Get or create a namespace for the given repository.
        """
        repo_info, _created = RepositoryInfo.objects.get_or_create(
            external_id=repository.pk, defaults={"client": repository.client, "external_slug": repository.slug}
        )
        try:
            latest_namespace = self.filter(repository_info=repo_info).latest()
        except CodebaseNamespace.DoesNotExist:
            latest_namespace = self.create(
                repository_info=repo_info, sha=repository.head_sha, tracking_branch=repository.default_branch
            )
            return latest_namespace, True
        else:
            return latest_namespace, False


class RepositoryInfo(TimeStampedModel):
    """
    This model stores information about a repository in an external source control system.
    """

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True)
    external_slug = models.CharField(max_length=256)
    external_id = models.CharField(max_length=256)
    client = models.CharField(max_length=16, choices=ClientChoices.choices)

    def __str__(self) -> str:
        return self.external_slug


class CodebaseNamespace(TimeStampedModel):
    """
    This model stores information about a namespace in a repository.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        INDEXING = "indexing", "Indexing"
        INDEXED = "indexed", "Indexed"
        FAILED = "failed", "Failed"

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True)
    repository_info = models.ForeignKey(RepositoryInfo, on_delete=models.CASCADE, related_name="namespaces")
    sha = models.CharField(max_length=64)
    tracking_branch = models.CharField(max_length=256, blank=True)
    status = models.CharField(max_length=16, default=Status.PENDING, choices=Status.choices)

    objects: CodebaseNamespaceManager = CodebaseNamespaceManager()

    class Meta:
        get_latest_by = "created"

    def __str__(self) -> str:
        return self.sha
