import re
from typing import cast

from django.db.models import QuerySet

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pgvector.django import CosineDistance
from pydantic import Field
from tantivy import Index

from codebase.models import CodebaseDocument, CodebaseNamespace

variable_pattern = re.compile(r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))")


class TantityRetriever(BaseRetriever):
    """
    Retriever based on Tantivy.
    """

    index: Index
    k: int = 10
    search_kwargs: dict = Field(default_factory=dict)

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        """
        Get the relevant documents for the query.

        Args:
            query (str): The query.
            run_manager (CallbackManagerForRetrieverRun): The run manager.

        Returns:
            list[Document]: The relevant documents.
        """
        self.index.reload()
        searcher = self.index.searcher()
        parsed_query = self.index.parse_query(self._tokenize_code(query), ["page_content"])
        results = []
        for _score, best_doc_address in searcher.search(parsed_query, self.k, **self.search_kwargs).hits:
            document = searcher.doc(best_doc_address)
            results.append(
                Document(
                    id=cast("str", document.get_first("doc_id")),
                    page_content=cast("str", document.get_first("page_content")),
                    metadata={
                        "id": cast("str", document.get_first("doc_id")),
                        "retriever": self.__class__.__name__,
                        "score": _score,
                        **cast("dict", document.get_first("page_metadata")),
                    },
                )
            )
        return results

    def _tokenize_code(self, code: str) -> str:
        """
        Tokenize the code snippet.

        Args:
            code (str): The code snippet.

        Returns:
            str: The tokenized code snippet.
        """
        matches = re.finditer(r"\b\w{2,}\b", code)
        tokens = []
        for m in matches:
            text = m.group()

            for section in text.split("_"):
                for part in variable_pattern.findall(section):
                    if len(part) < 2:
                        continue
                    # if more than half of the characters are letters
                    # and the ratio of unique characters to the number of characters is less than 5
                    if (
                        sum(1 for c in part if "a" <= c <= "z" or "A" <= c <= "Z" or "0" <= c <= "9") > len(part) // 2
                        and len(part) / len(set(part)) < 4
                    ):
                        tokens.append(part.lower())

        return " ".join(tokens)


class PostgresRetriever(BaseRetriever):
    """
    Retriever based on Postgres with PGVector.
    """

    embeddings: Embeddings
    k: int = 10
    search_kwargs: dict = Field(default_factory=dict)

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        """
        Get the relevant documents for the query.

        Args:
            query (str): The query.
            run_manager (CallbackManagerForRetrieverRun): The run manager.

        Returns:
            list[Document]: The relevant documents.
        """
        return [
            Document(
                id=document.uuid,
                page_content=document.page_content,
                metadata={
                    "id": str(document.uuid),
                    "retriever": self.__class__.__name__,
                    "score": document.distance,
                    **document.metadata,
                },
            )
            for document in self._get_documents_queryset()
            .annotate(distance=CosineDistance("page_content_vector", self.embeddings.embed_query(query)))
            .filter(**self.search_kwargs)
            .order_by("distance")[: self.k]
        ]

    def _get_documents_queryset(self) -> QuerySet[CodebaseDocument]:
        """
        Get the documents queryset.
        """
        return CodebaseDocument.objects.all()


class ScopedPostgresRetriever(PostgresRetriever):
    """
    Retriever based on Postgres with PGVector with a scoped namespace.
    """

    namespace: CodebaseNamespace

    def _get_documents_queryset(self) -> QuerySet[CodebaseDocument]:
        """
        Get the documents queryset.
        """
        return self.namespace.documents.all()
