from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tantivy import Document, Index, SchemaBuilder

from codebase.search_engines.base import SearchEngine
from codebase.search_engines.retrievers import ScopedTantityRetriever, TantityRetriever
from daiv.settings.components import DATA_DIR

if TYPE_CHECKING:
    from langchain_core.documents import Document as LangDocument

    from codebase.models import CodebaseNamespace

# IMPORTANT: if we change the schema, we need to increment the version of the index
TANTIVY_INDEX_PATH = DATA_DIR / "tantivy_index_v1"

variable_pattern = re.compile(r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))")


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
        self.index = self._get_index(persistent)

    def _get_index(self, persistent: bool = True) -> Index:
        """
        Creates and returns a Tantivy search index.

        Args:
            persistent: If True, store index on disk; if False, create in-memory index

        Returns:
            A configured Tantivy Index instance with text fields for document storage and searching
        """
        schema_builder = SchemaBuilder()

        # index_option needs to be "position" (default) to fields that are filtered by
        schema_builder.add_text_field("doc_id", stored=True, index_option="basic", tokenizer_name="raw")
        schema_builder.add_text_field("page_content", stored=True)
        schema_builder.add_text_field("page_source", stored=True)
        schema_builder.add_json_field("page_metadata", stored=True, index_option="basic")
        schema_builder.add_text_field("repo_id", tokenizer_name="raw")  # avoid tokenizing
        schema_builder.add_text_field("ref", tokenizer_name="raw")  # avoid tokenizing
        schema = schema_builder.build()

        if persistent and not TANTIVY_INDEX_PATH.exists():
            TANTIVY_INDEX_PATH.mkdir(parents=True, exist_ok=True)

        return Index(schema, path=persistent and TANTIVY_INDEX_PATH.as_posix() or None)

    def add_documents(self, namespace: CodebaseNamespace, documents: list[LangDocument]):
        """
        Adds documents to the search index.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            documents: List of LangDocument objects to index

        Note:
            Each document must have an id, source metadata, and page_content.
        """
        writer = self.index.writer()
        for document in documents:
            if document.metadata.get("content_type") == "simplified_code":
                continue

            writer.add_document(
                Document(
                    doc_id=document.id,
                    page_metadata=document.metadata,
                    page_source=document.metadata["source"],
                    page_content=document.page_content,
                    repo_id=namespace.repository_info.external_slug,
                    ref=namespace.tracking_ref,
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

        docs_to_delete = []

        self.index.reload()
        searcher = self.index.searcher()

        for page_source in source:
            for _score, best_doc_address in searcher.search(
                self.index.parse_query(
                    f'repo_id:"{namespace.repository_info.external_slug}" '
                    f'AND ref:"{namespace.tracking_ref}" '
                    f'AND page_source:"{page_source}"'
                )
            ).hits:
                document = searcher.doc(best_doc_address)
                docs_to_delete.append(document.get_first("doc_id"))

        writer = self.index.writer()
        for doc_id in docs_to_delete:
            writer.delete_documents("doc_id", doc_id)
        writer.commit()

    def delete(self, namespace: CodebaseNamespace):
        """
        Deletes all documents from the namespace's index.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
        """
        writer = self.index.writer()
        writer.delete_documents("repo_id", namespace.repository_info.external_slug)
        writer.commit()

    def as_retriever(self, namespace: CodebaseNamespace | None = None, **kwargs) -> TantityRetriever:
        """
        Creates a retriever instance for searching the index.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            **kwargs: Additional configuration parameters for the retriever

        Returns:
            A configured TantityRetriever instance for the specified namespace
        """
        if namespace is None:
            return TantityRetriever(index=self.index, **kwargs)
        return ScopedTantityRetriever(namespace=namespace, index=self.index, **kwargs)
