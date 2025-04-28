from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import models

from django_extensions.db.models import TimeStampedModel
from langchain_core.documents import Document
from pgvector.django import HnswIndex, VectorField

if TYPE_CHECKING:
    from .base import Repository


class ClientChoices(models.TextChoices):
    GITLAB = "gitlab", "GitLab"
    GITHUB = "github", "GitHub"


class CodebaseNamespaceManager(models.Manager):
    def get_or_create_from_repository(
        self, repository: Repository, *, tracking_ref: str, head_sha: str
    ) -> tuple[CodebaseNamespace, bool]:
        """
        Get or create a namespace for the given repository.
        """
        # We could use get_or_create here, but we want to ensure that the repository info is always updated
        # with the latest information from the repository, in particular the external_slug that can change
        # over time.
        repo_info, _created = RepositoryInfo.objects.update_or_create(
            external_id=repository.pk, defaults={"client": repository.client, "external_slug": repository.slug}
        )
        try:
            latest_namespace = self.filter(
                repository_info=repo_info, tracking_ref=tracking_ref, status=CodebaseNamespace.Status.INDEXED
            ).latest()
        except CodebaseNamespace.DoesNotExist:
            latest_namespace = self.create(repository_info=repo_info, sha=head_sha, tracking_ref=tracking_ref)
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
    tracking_ref = models.CharField(max_length=256, blank=True)
    status = models.CharField(max_length=16, default=Status.PENDING, choices=Status.choices)

    objects: CodebaseNamespaceManager = CodebaseNamespaceManager()

    class Meta:
        get_latest_by = "created"

    def __str__(self) -> str:
        return self.sha


class CodebaseDocument(TimeStampedModel):
    """
    This model stores information about a document in a namespace.
    """

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True)
    namespace = models.ForeignKey(CodebaseNamespace, on_delete=models.CASCADE, related_name="documents")
    source = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    page_content = models.TextField()
    # This will accept vectors of any dimension on first insert
    page_content_vector = VectorField(dimensions=None)
    is_default_branch = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict)

    class Meta:
        indexes = [
            HnswIndex(
                name="document_hnsw_index",
                fields=["page_content_vector"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            )
        ]

    def __str__(self) -> str:
        return self.source

    def as_document(self) -> Document:
        return Document(page_content=self.page_content, metadata=self.metadata)
