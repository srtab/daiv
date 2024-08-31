import functools

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from codebase.conf import settings
from codebase.search_engines.base import ScoredResult, SearchEngine


@functools.cache
def embedding_function() -> OpenAIEmbeddings:
    """
    Return the OpenAI embeddings function.
    """
    return OpenAIEmbeddings(model="text-embedding-3-small", chunk_size=500)


class SemanticSearchEngine(SearchEngine):
    """
    Semantic search engine based on Chroma.
    """

    def __init__(self, collection_name: str):
        self.collection_name = collection_name
        self.embedding = embedding_function()
        self.db = Chroma(embedding_function=self.embedding, **self._chroma_common_kwargs())

    def add_documents(self, index_name: str, documents: list[Document]):
        """
        Add documents to the index.

        Args:
            index_name (str): The name of the index. Not relevant for Chroma.
            documents (list[Document]): The documents to be added.
        """
        Chroma.from_documents(documents=documents, embedding=self.embedding, **self._chroma_common_kwargs())

    def delete_documents(self, index_name: str, field_name: str, field_value: str | list[str]):
        """
        Delete documents from the index by a field value.

        Args:
            index_name (str): The name of the index.
            field_name (str): The field name.
            field_value (str): The field value.
        """
        if isinstance(field_value, str):
            field_value = [field_value]

        self.db.delete(where={"$and": [{"repo_id": index_name}, {field_name: {"$in": field_value}}]})

    def get_documents(self, index_name: str, field_name: str, field_value: str | list[str]) -> list[Document]:
        """
        Get documents from the index by a field value.

        Args:
            index_name (str): The name of the index.
            field_name (str): The field name.
            field_value (str): The field value.

        Returns:
            list[Document]: The documents.
        """
        if isinstance(field_value, str):
            field_value = [field_value]

        results = self.db.get(where={"$and": [{"repo_id": index_name}, {field_name: {"$in": field_value}}]})
        return [
            Document(page_content=document, metadata=results["metadata"][index])
            for index, document in enumerate(results["documents"])
        ]

    def delete(self, index_name: str):
        """
        Delete all documents from the index.

        Args:
            index_name (str): The name of the index.
        """
        results = self.db.get(where={"repo_id": index_name})
        for document_id in results["ids"]:
            self.db.delete(document_id)

    def search(self, index_name: str, query: str, k: int = 10, **kwargs) -> list[ScoredResult]:
        """
        Search the index and return the top-k scored results.

        Args:
            index_name (str): The name of the index.
            query (str): The query.
            k (int): The number of results to return.
            **kwargs: Additional search parameters to pass to Chroma.

        Returns:
            list[ScoredResult]: The scored results.
        """
        conditions: list[dict[str, str]] = [{"repo_id": index_name}]

        if content_type := kwargs.pop("content_type", None):
            assert content_type in ["functions_classes", "simplified_code"], "Invalid content type."
            conditions.append({"content_type": content_type})

        chroma_filter: dict[str, str | list | dict[str, str]] = {}
        if len(conditions) > 1:
            chroma_filter = {"$and": conditions}
        elif len(conditions) == 1:
            chroma_filter = conditions[0]

        results = self.db.similarity_search_with_relevance_scores(query, k=k, filter=chroma_filter, **kwargs)

        if not results:
            return []

        return [ScoredResult(score=score, document=document) for document, score in results]

    def _chroma_common_kwargs(self) -> dict:
        """
        Return the common kwargs for Chroma.
        """
        return {
            "client": chromadb.HttpClient(
                host=settings.CODEBASE_CHROMA_HOST,
                port=settings.CODEBASE_CHROMA_PORT,
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            ),
            "collection_name": self.collection_name,
            "collection_metadata": {"hnsw:space": "cosine", "hnsw:sync_threshold": 2000, "hnsw:batch_size": 500},
        }
