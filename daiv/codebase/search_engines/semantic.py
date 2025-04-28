import functools
import logging
from textwrap import dedent

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import OpenAIEmbeddings
from langchain_voyageai.embeddings import DEFAULT_VOYAGE_3_BATCH_SIZE, VoyageAIEmbeddings
from langsmith import tracing_context
from pydantic import SecretStr

from codebase.conf import settings
from codebase.models import CodebaseDocument, CodebaseNamespace
from codebase.search_engines.base import SearchEngine
from codebase.search_engines.retrievers import PostgresRetriever, ScopedPostgresRetriever
from daiv.settings.components import DATA_DIR

logger = logging.getLogger("daiv.indexes.semantic")


@functools.cache
def embeddings_function() -> Embeddings:
    """
    Creates and returns a cached embeddings function.

    Returns:
        Embeddings: Configured embeddings model with optimized chunk size.
    """
    provider, model_name = settings.EMBEDDINGS_MODEL_NAME.split("/", 1)

    common_kwargs = {}
    if settings.EMBEDDINGS_API_KEY:
        common_kwargs["api_key"] = SecretStr(settings.EMBEDDINGS_API_KEY)

    if provider == "openai":
        return OpenAIEmbeddings(
            model=model_name,
            dimensions=settings.EMBEDDINGS_DIMENSIONS,
            chunk_size=settings.EMBEDDINGS_BATCH_SIZE,
            **common_kwargs,
        )
    elif provider == "huggingface":
        return HuggingFaceEmbeddings(model_name=model_name, cache_folder=str(DATA_DIR / "embeddings"))
    elif provider == "voyageai":
        return VoyageAIEmbeddings(
            model=model_name,
            output_dimension=settings.EMBEDDINGS_DIMENSIONS if settings.EMBEDDINGS_DIMENSIONS != 1536 else 1024,
            batch_size=DEFAULT_VOYAGE_3_BATCH_SIZE,
            **common_kwargs,
        )
    else:
        raise ValueError(f"Unsupported embeddings provider: {provider}")


class SemanticSearchEngine(SearchEngine):
    """
    Semantic search engine implementation using vector embeddings stored in Postgres.
    """

    def __init__(self, augmented_context: bool = False):
        from automation.agents.code_describer import CodeDescriberAgent

        self.embeddings = embeddings_function()
        self.code_describer = CodeDescriberAgent().agent
        self.augmented_context = augmented_context

    def add_documents(self, namespace: CodebaseNamespace, documents: list[Document]):
        """
        Adds documents to the vector store after computing their embeddings.

        Args:
            namespace: CodebaseNamespace where the documents will be stored
            documents: List of documents to be processed and stored

        Returns:
            list[CodebaseDocument]: The created document records
        """
        if self.augmented_context:
            logger.info("Augmenting context...")

            with tracing_context(enabled=False):
                # Avoid tracing the code describer as it can be very overwhelming and fill up the trace store
                described_documents = self.code_describer.batch([
                    {
                        "code": document.page_content,
                        "filename": document.metadata.get("source", ""),
                        "language": document.metadata.get("language", "Not specified"),
                    }
                    for document in documents
                ])
        else:
            described_documents = [""] * len(documents)

        zipped_documents = zip(documents, described_documents, strict=True)

        logger.info("Creating embeddings...")
        document_vectors = self.embeddings.embed_documents([
            self._build_content_to_embed(document, description) for document, description in zipped_documents
        ])

        logger.info("Persisting documents...")
        return CodebaseDocument.objects.bulk_create([
            CodebaseDocument(
                namespace=namespace,
                source=document.metadata.get("source", ""),
                description=description,
                page_content=document.page_content,
                page_content_vector=page_content_vector,
                is_default_branch=document.metadata.get("default_branch") == document.metadata.get("ref"),
                metadata=document.metadata,
            )
            for document, description, page_content_vector in zip(
                documents, described_documents, document_vectors, strict=True
            )
        ])

    def _build_content_to_embed(self, document: Document, description: str) -> str:
        """
        Add contextual information to the content to embed.

        Args:
            document: Document to add contextual information to
            description: Description of the document

        Returns:
            str: Content to embed
        """
        if not description:
            return dedent(f"""\
                Repository: {document.metadata.get("repo_id", "")}
                File Path: {document.metadata.get("source", "")}

                {document.page_content}
            """)
        else:
            return dedent(f"""\
                Repository: {document.metadata.get("repo_id", "")}
                File Path: {document.metadata.get("source", "")}
                Description: {description}

                {document.page_content}
            """)

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

    def as_retriever(self, namespace: CodebaseNamespace | None = None, **kwargs) -> BaseRetriever:
        """
        Creates a retriever instance for semantic similarity search.

        Args:
            namespace: CodebaseNamespace to search in or `None` to search in all namespaces
            **kwargs: Additional parameters to configure the retriever

        Returns:
            BaseRetriever: Configured retriever instance
        """
        if namespace is None:
            return PostgresRetriever(embeddings=self.embeddings, **kwargs)
        return ScopedPostgresRetriever(namespace=namespace, embeddings=self.embeddings, **kwargs)
