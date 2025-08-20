import logging
import re
import uuid
from collections.abc import Generator, Iterable, Sequence
from itertools import islice
from textwrap import dedent
from typing import Any

import inflection
import tiktoken
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStoreRetriever
from langchain_qdrant import FastEmbedSparse, RetrievalMode
from langchain_qdrant import QdrantVectorStore as LangChainQdrantVectorStore
from qdrant_client import QdrantClient, models
from qdrant_client.http.models import Distance, SparseVectorParams, VectorParams

from codebase.conf import settings
from codebase.models import CodebaseNamespace

from .base import SearchEngine
from .utils import embeddings_function

logger = logging.getLogger("daiv.indexes")

COLLECTION_NAME = "codebase"


class QdrantVectorStore(LangChainQdrantVectorStore):
    """
    QdrantVectorStore implementation that supports hybrid retrieval.

    Rewritten to support different text content for dense and sparse embeddings.
    """

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        filter: models.Filter | None = None,  # noqa: A002
        search_params: models.SearchParams | None = None,
        offset: int = 0,
        score_threshold: float | None = None,
        consistency: models.ReadConsistency | None = None,
        hybrid_fusion: models.FusionQuery | None = None,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        """Return docs most similar to query.

        Returns:
            List of documents most similar to the query text and distance for each.
        """

        query_options = {
            "collection_name": self.collection_name,
            "query_filter": filter,
            "search_params": search_params,
            "limit": k * 2,
            "offset": offset,
            "with_payload": True,
            "with_vectors": False,
            "score_threshold": score_threshold,
            "consistency": consistency,
            **kwargs,
        }

        query_dense_embedding = self.embeddings.embed_query(query)
        query_sparse_embedding = self.sparse_embeddings.embed_query(self._textify(query))

        results = self.client.query_points(
            prefetch=[
                models.Prefetch(
                    using=self.vector_name, query=query_dense_embedding, filter=filter, limit=k, params=search_params
                ),
                models.Prefetch(
                    using=self.sparse_vector_name,
                    query=models.SparseVector(
                        indices=query_sparse_embedding.indices, values=query_sparse_embedding.values
                    ),
                    filter=filter,
                    limit=k,
                    params=search_params,
                ),
            ],
            query=hybrid_fusion or models.FusionQuery(fusion=models.Fusion.RRF),
            **query_options,
        ).points

        return [
            (
                self._document_from_point(
                    result, self.collection_name, self.content_payload_key, self.metadata_payload_key
                ),
                result.score,
            )
            for result in results
        ]

    @classmethod
    def _document_from_point(
        cls, scored_point: Any, collection_name: str, content_payload_key: str, metadata_payload_key: str
    ) -> Document:
        """
        Convert a point to a document.
        """
        metadata = scored_point.payload.get(metadata_payload_key) or {}
        metadata["_id"] = scored_point.id
        metadata["_score"] = scored_point.score
        metadata["_collection_name"] = collection_name
        return Document(
            id=scored_point.id, page_content=scored_point.payload.get(content_payload_key, ""), metadata=metadata
        )

    def _generate_batches(
        self,
        texts: Iterable[str],
        metadatas: list[dict] | None = None,
        ids: Sequence[str | int] | None = None,
        batch_size: int = 64,
    ) -> Generator[tuple[list[str | int], list[models.PointStruct]], Any]:
        """
        Generate batches of points for the given texts.

        Args:
            texts: Iterable of texts to generate batches for
            metadatas: List of metadata dictionaries for each text
            ids: List of ids for each text

        Returns:
            Generator of tuples containing batch ids and points
        """
        texts_iterator = iter(texts)
        metadatas_iterator = iter(metadatas or [])
        ids_iterator = iter(ids or [uuid.uuid4().hex for _ in iter(texts)])

        while batch_texts := list(islice(texts_iterator, batch_size)):
            batch_metadatas = list(islice(metadatas_iterator, batch_size)) or None
            batch_ids = list(islice(ids_iterator, batch_size))
            points = [
                models.PointStruct(id=point_id, vector=vector, payload=payload)
                for point_id, vector, payload in zip(
                    batch_ids,
                    self._build_vectors(batch_texts, batch_metadatas),
                    self._build_payloads(
                        batch_texts, batch_metadatas, self.content_payload_key, self.metadata_payload_key
                    ),
                    strict=False,
                )
            ]

            yield batch_ids, points

    def _build_vectors(self, texts: Iterable[str], metadatas: list[dict]) -> list[models.VectorStruct]:
        """
        Build vectors for the given texts.

        Args:
            texts: Iterable of texts to build vectors for
            metadatas: List of metadata dictionaries for each text

        Returns:
            List of vectors
        """
        dense_code = list(map(self._build_dense_content_to_embed, list(texts), metadatas))
        dense_texts = list(map(self._textify, dense_code))

        dense_code_embeddings = self.embeddings.embed_documents(dense_code)
        sparse_embeddings = self.sparse_embeddings.embed_documents(dense_texts)

        assert len(dense_code_embeddings) == len(sparse_embeddings), (
            "Mismatched length between dense and sparse embeddings."
        )

        return [
            {
                self.vector_name: dense_code_vector,
                self.sparse_vector_name: models.SparseVector(
                    values=sparse_vector.values, indices=sparse_vector.indices
                ),
            }
            for dense_code_vector, sparse_vector in zip(dense_code_embeddings, sparse_embeddings, strict=True)
        ]

    def _build_dense_content_to_embed(self, text: str, metadata: dict) -> str:
        """
        Add contextual information to the content to embed.

        Args:
            text: Text to add contextual information to
            metadata: Metadata dictionary for the document

        Returns:
            str: Content to embed
        """

        content = dedent(f"""\
            Repository: {metadata.get("repo_id", "")}
            File Path: {metadata.get("source", "")}

            {text}""")

        count = self._embeddings_count_tokens(content)

        if count > settings.EMBEDDINGS_MAX_INPUT_TOKENS:
            logger.warning(
                "Chunk is too large, truncating: %s. Chunk tokens: %d, max allowed: %d",
                metadata["source"],
                self._embeddings_count_tokens(content),
                settings.EMBEDDINGS_MAX_INPUT_TOKENS,
            )
            return content[: settings.EMBEDDINGS_MAX_INPUT_TOKENS]

        return content

    def _embeddings_count_tokens(self, text: str) -> int:
        """
        Count the number of tokens in the text.
        """
        provider, model_name = settings.EMBEDDINGS_MODEL_NAME.split("/", 1)

        if provider == "voyageai":
            return self.embeddings._client.count_tokens([text], model=model_name)
        elif provider == "openai":
            return len(tiktoken.encoding_for_model(model_name).encode(text))
        return len(tiktoken.get_encoding("cl100k_base").encode(text))

    def _textify(self, text: str) -> str:
        """
        Textify the text.
        """
        text_repr = inflection.underscore(text)

        tokens = re.split(r"\W", text_repr)
        tokens = filter(lambda x: x, tokens)
        return " ".join(tokens)


class HybridSearchEngine(SearchEngine):
    """
    Hybrid search engine implementation using Qdrant.
    """

    client: QdrantClient
    store: QdrantVectorStore

    def __init__(self):
        client = QdrantClient("http://qdrant:6333")

        if not client.collection_exists(COLLECTION_NAME):
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=settings.EMBEDDINGS_DIMENSIONS, distance=Distance.COSINE, on_disk=True
                ),
                sparse_vectors_config={
                    QdrantVectorStore.SPARSE_VECTOR_NAME: SparseVectorParams(
                        index=models.SparseIndexParams(), modifier=models.Modifier.IDF
                    )
                },
                hnsw_config=models.HnswConfigDiff(m=64, ef_construct=512),
            )
            client.create_payload_index(
                COLLECTION_NAME, field_name="metadata.repo_id", field_schema=models.PayloadSchemaType.KEYWORD
            )
            client.create_payload_index(
                COLLECTION_NAME, field_name="metadata.ref", field_schema=models.PayloadSchemaType.KEYWORD
            )
            client.create_payload_index(
                COLLECTION_NAME, field_name="metadata.source", field_schema=models.PayloadSchemaType.KEYWORD
            )

        self.store = QdrantVectorStore(
            client=client,
            collection_name=COLLECTION_NAME,
            embedding=embeddings_function(),
            sparse_embedding=FastEmbedSparse(model_name="Qdrant/bm42-all-minilm-l6-v2-attentions", parallel=2),
            retrieval_mode=RetrievalMode.HYBRID,
        )

    async def add_documents(self, namespace: CodebaseNamespace, documents: list[Document]):
        """
        Add documents to the search engine.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            documents: List of documents to add
        """
        await self.store.aadd_documents(documents, batch_size=settings.EMBEDDINGS_BATCH_SIZE)

    async def delete_documents(self, namespace: CodebaseNamespace, source: str | list[str]):
        """
        Delete documents from the search engine.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            source: Source of the documents to delete
        """
        if isinstance(source, str):
            source = [source]

        await self.store.adelete(
            ids=models.Filter(
                must=[
                    models.FieldCondition(
                        key="metadata.repo_id", match=models.MatchValue(value=namespace.repository_info.external_slug)
                    ),
                    models.FieldCondition(key="metadata.ref", match=models.MatchValue(value=namespace.tracking_ref)),
                    models.FieldCondition(key="metadata.source", match=models.MatchAny(any=source)),
                ]
            )
        )

    def delete(self, namespace: CodebaseNamespace):
        """
        Delete all documents for a given repository.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
        """
        self.store.delete(
            ids=models.Filter(
                must=[
                    models.FieldCondition(
                        key="metadata.repo_id", match=models.MatchValue(value=namespace.repository_info.external_slug)
                    )
                ]
            )
        )

    def as_retriever(self, namespace: CodebaseNamespace | None = None) -> VectorStoreRetriever:
        """
        Create a retriever for the search engine.

        Args:
            namespace: CodebaseNamespace containing repository and reference information
            k: Number of results to return

        Returns:
            A retriever for the search engine
        """
        search_kwargs = {"k": 10, "search_params": models.SearchParams(hnsw_ef=128)}
        if namespace is not None:
            search_kwargs["filter"] = models.Filter(
                must=[
                    models.FieldCondition(
                        key="metadata.repo_id", match=models.MatchValue(value=namespace.repository_info.external_slug)
                    )
                ]
            )

        return self.store.as_retriever(search_kwargs=search_kwargs)
