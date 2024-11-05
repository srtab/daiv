import functools

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_openai import OpenAIEmbeddings

from codebase.models import CodebaseDocument, CodebaseNamespace
from codebase.search_engines.base import SearchEngine
from codebase.search_engines.retrievers import PostgresRetriever

EMBEDDING_MODEL_NAME = "text-embedding-3-small"


@functools.cache
def embeddings_function() -> OpenAIEmbeddings:
    """
    Creates and returns a cached OpenAI embeddings function.

    Returns:
        OpenAIEmbeddings: Configured embeddings model with optimized chunk size.
    """
    return OpenAIEmbeddings(model=EMBEDDING_MODEL_NAME, chunk_size=500)


class SemanticSearchEngine(SearchEngine):
    """
    Semantic search engine implementation using vector embeddings stored in Postgres.
    """

    def __init__(self):
        self.embeddings = embeddings_function()

    def add_documents(self, namespace: CodebaseNamespace, documents: list[Document]):
        """
        Adds documents to the vector store after computing their embeddings.

        Args:
            namespace: CodebaseNamespace where the documents will be stored
            documents: List of documents to be processed and stored

        Returns:
            list[CodebaseDocument]: The created document records
        """
        document_vectors = self.embeddings.embed_documents([document.page_content for document in documents])

        return CodebaseDocument.objects.bulk_create([
            CodebaseDocument(
                namespace=namespace,
                source=document.metadata.get("source", ""),
                page_content=document.page_content,
                page_content_vector=vector,
                metadata=document.metadata,
            )
            for document, vector in zip(documents, document_vectors, strict=True)
        ])

    def delete_documents(self, namespace: CodebaseNamespace, source: str | list[str]):
        """
        Deletes documents from the namespace matching the given source(s).

        Args:
            namespace: CodebaseNamespace containing the documents
            source: Single source path or list of source paths to match for deletion
        """
        if isinstance(source, str):
            source = [source]

        namespace.documents.filter(source__in=source).delete()

    def get_documents(self, namespace: CodebaseNamespace, source: str | list[str]) -> list[Document]:
        """
        Retrieves documents from the namespace matching the given source(s).

        Args:
            namespace: CodebaseNamespace to search in
            source: Single source path or list of source paths to retrieve

        Returns:
            list[Document]: The matching documents
        """
        if isinstance(source, str):
            source = [source]

        return [document.as_document(document) for document in namespace.documents.filter(source__in=source)]

    def delete(self, namespace: CodebaseNamespace):
        """
        Deletes all documents from the given namespace.

        Args:
            namespace: CodebaseNamespace to clear
        """
        namespace.documents.all().delete()

    def as_retriever(self, namespace: CodebaseNamespace, **kwargs) -> BaseRetriever:
        """
        Creates a retriever instance for semantic similarity search.

        Args:
            namespace: CodebaseNamespace to search in
            **kwargs: Additional parameters to configure the retriever

        Returns:
            BaseRetriever: Configured retriever instance
        """
        return PostgresRetriever(namespace=namespace, embeddings=self.embeddings, **kwargs)
