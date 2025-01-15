from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import models

from django_extensions.db.models import TimeStampedModel
from langchain_core.documents import Document
from pgvector.django import HnswIndex, VectorField

if TYPE_CHECKING:
    from langchain_core.embeddings import Embeddings

    from codebase.base import Repository


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
        repo_info, _created = RepositoryInfo.objects.get_or_create(
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

    def add_documents(self, documents: list[Document], embedding: Embeddings) -> list[CodebaseDocument]:
        """
        Add documents to the index.
        """
        return CodebaseDocument.objects.create_from_documents(self, documents, embedding)


class CodebaseDocumentManager(models.Manager):
    def create_from_documents(
        self, namespace: CodebaseNamespace, documents: list[Document], embedding: Embeddings
    ) -> list[CodebaseDocument]:
        """
        Create documents from a list of documents using bulk_create.
        """
        document_vectors = embedding.embed_documents([document.page_content for document in documents])
        return CodebaseDocument.objects.bulk_create([
            CodebaseDocument(
                uuid=document.id,
                namespace=namespace,
                source=document.metadata.get("source", ""),
                page_content=document.page_content,
                page_content_vector=vector,
                metadata=document.metadata,
            )
            for document, vector in zip(documents, document_vectors, strict=True)
        ])


class CodebaseDocument(TimeStampedModel):
    """
    This model stores information about a document in a namespace.
    """

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True)
    namespace = models.ForeignKey(CodebaseNamespace, on_delete=models.CASCADE, related_name="documents")
    source = models.CharField(max_length=256)
    source_vector = VectorField(dimensions=1536)
    page_content = models.TextField()
    page_content_vector = VectorField(dimensions=1536)
    metadata = models.JSONField(default=dict)

    objects: CodebaseDocumentManager = CodebaseDocumentManager()

    class Meta:
        indexes = [
            HnswIndex(
                name="document_hnsw_index",
                fields=["page_content_vector"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
            HnswIndex(
                name="source_hnsw_index",
                fields=["source_vector"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return self.source

    def as_document(self) -> Document:
        return Document(page_content=self.page_content, metadata=self.metadata)
