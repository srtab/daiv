from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

from django.utils.text import slugify

from tantivy import Document, Index, SchemaBuilder

from codebase.search_engines.base import SearchEngine
from codebase.search_engines.retrievers import TantityRetriever
from daiv.settings.components import DATA_DIR

if TYPE_CHECKING:
    from langchain_core.documents import Document as LangDocument

    from codebase.models import CodebaseNamespace

TANTIVY_INDEX_PATH = DATA_DIR / "tantivy_index"

variable_pattern = re.compile(r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))")


@lru_cache
def tantivy_index(index_name: str, persistent: bool = True) -> Index:
    """
    Creates and returns a Tantivy search index.

    Args:
        index_name: Name of the index to create or retrieve
        persistent: If True, store index on disk; if False, create in-memory index

    Returns:
        A configured Tantivy Index instance with text fields for document storage and searching
    """
    schema_builder = SchemaBuilder()
    schema_builder.add_text_field("doc_id", stored=True, index_option="basic")
    schema_builder.add_text_field("page_content", stored=True)
    schema_builder.add_text_field("page_source", stored=True, index_option="basic")
    schema_builder.add_json_field("page_metadata", stored=True, index_option="basic")
    schema = schema_builder.build()

    index_path = TANTIVY_INDEX_PATH / slugify(index_name)
    if persistent and not index_path.exists():
        index_path.mkdir(parents=True, exist_ok=True)

    return Index(schema, path=persistent and index_path.as_posix() or None)


class LexicalSearchEngine(SearchEngine):
    """
    Tantivy-based lexical search engine implementation.

    Provides document indexing and retrieval capabilities using the Tantivy search engine.
    Supports both persistent and in-memory indices.
    """

    def __init__(self, persistent: bool = True):
        """Initializes the lexical search engine.

        Args:
            persistent: If True, indices are stored on disk; if False, indices are in-memory
        """
        self.persistent = persistent

    def add_documents(self, namespace: CodebaseNamespace, documents: list[LangDocument]):
        """
        Adds documents to the search index.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            documents: List of LangDocument objects to index

        Note:
            Each document must have an id, source metadata, and page_content.
        """
        writer = self._get_index(f"{namespace.repository_info.external_slug}:{namespace.tracking_ref}").writer()
        for document in documents:
            if document.metadata.get("content_type") == "simplified_code":
                continue

            writer.add_document(
                Document(
                    page_metadata=document.metadata,
                    page_source=document.metadata["source"],
                    page_content=document.page_content,
                    doc_id=document.id,
                )
            )
        writer.commit()

    def delete_documents(self, namespace: CodebaseNamespace, source: str | list[str]):
        """
        Deletes documents from the index based on their source.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            source: Single source string or list of source strings identifying documents to delete
        """
        if isinstance(source, str):
            source = [source]

        writer = self._get_index(f"{namespace.repository_info.external_slug}:{namespace.tracking_ref}").writer()
        for value in source:
            writer.delete_documents("page_source", value)
        writer.commit()

    def delete(self, namespace: CodebaseNamespace):
        """
        Deletes all documents from the namespace's index.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
        """
        writer = self._get_index(f"{namespace.repository_info.external_slug}:{namespace.tracking_ref}").writer()
        writer.delete_all_documents()
        writer.commit()

    def as_retriever(self, namespace: CodebaseNamespace, **kwargs) -> TantityRetriever:
        """
        Creates a retriever instance for searching the index.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            **kwargs: Additional configuration parameters for the retriever

        Returns:
            A configured TantityRetriever instance for the specified namespace
        """
        index = self._get_index(f"{namespace.repository_info.external_slug}:{namespace.tracking_ref}")
        return TantityRetriever(index=index, **kwargs)

    def _get_index(self, index_name: str) -> Index:
        """
        Retrieves or creates a Tantivy index.

        Args:
            index_name: Name of the index to retrieve or create

        Returns:
            A Tantivy Index instance
        """
        return tantivy_index(index_name, self.persistent)
