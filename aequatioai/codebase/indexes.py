from __future__ import annotations

import abc
import functools
import logging
from typing import TYPE_CHECKING, Literal, cast

from django.db import transaction
from django.db.models import Q

import chromadb
import chromadb.config
from langchain_chroma import Chroma
from langchain_community.document_loaders.blob_loaders import Blob
from langchain_community.document_loaders.parsers.language import LanguageParser
from langchain_openai import OpenAIEmbeddings

from codebase.conf import settings
from codebase.document_loaders import GenericLanguageLoader
from codebase.models import CodebaseNamespace

if TYPE_CHECKING:
    from langchain_core.documents import Document

    from codebase.base import RepositoryFile
    from codebase.clients import GitHubClient, GitLabClient

logger = logging.getLogger(__name__)


@functools.cache
def embedding_function() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(model="text-embedding-3-small", chunk_size=500)


class BaseCodebaseIndex(abc.ABC):
    """
    Base class for indexing data into Chroma.
    """

    repo_client: GitLabClient | GitHubClient
    collection_name: str

    def __init__(self, repo_client: GitLabClient | GitHubClient):
        self.repo_client = repo_client
        self.embedding = embedding_function()

    @abc.abstractmethod
    def update(self, repo_id: str):
        """
        Update the index in ChromaDB.
        """

    @property
    def db(self) -> Chroma:
        if not hasattr(self, "_db"):
            self._db = Chroma(embedding_function=self.embedding, **self.db_common_kwargs())
        return self._db

    def db_common_kwargs(self) -> dict:
        return {
            "client": chromadb.HttpClient(
                host=settings.CODEBASE_CHROMA_HOST,
                port=settings.CODEBASE_CHROMA_PORT,
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            ),
            "collection_name": self.collection_name,
            "collection_metadata": {"hnsw:space": "cosine", "hnsw:sync_threshold": 2000, "hnsw:batch_size": 500},
        }

    def reset(self, repo_id: str):
        """
        Reset the index of a repository.
        """
        logger.info("Reseting repo %s index.", repo_id)

        results = self.db.get(where={"repo_id": repo_id})
        for document_id in results["ids"]:
            self.db.delete(document_id)

        CodebaseNamespace.objects.filter(
            Q(repository_info__external_slug=repo_id) | Q(repository_info__external_id=repo_id)
        ).delete()

    def reset_all(self):
        """
        Reset all indexes.
        """
        logger.info("Reseting all indexes.")
        self.db.delete_collection()

        CodebaseNamespace.objects.all().delete()

    def delete_sources(self, repo_id: str, source_files: list[str]):
        """
        Delete documents by source.
        """
        results = self.db.get(where={"$and": [{"repo_id": repo_id}, {"source": {"$in": source_files}}]})
        for document_id in results["ids"]:
            self.db.delete(document_id)


class CodebaseIndex(BaseCodebaseIndex):
    """
    Index a codebase into Chroma.
    """

    collection_name: str = settings.CODEBASE_COLLECTION_NAME

    @transaction.atomic
    def update(self, repo_id: str):
        """
        Index a codebase into Chroma.
        """
        repository = self.repo_client.get_repository(repo_id)
        namespace, created = CodebaseNamespace.objects.get_or_create_from_repository(repository)

        if not created and namespace.sha == repository.head_sha:
            logger.info("Repo %s index already updated.", repo_id)
            return

        namespace.status = CodebaseNamespace.Status.INDEXING
        namespace.save(update_fields=["status", "modified"])

        try:
            loader_limit_paths_to = []

            # Check if the repository index needs to be partially updated
            if not created and namespace.sha != repository.head_sha:
                new_files, changed_files, deleted_files = self.repo_client.get_commit_changed_files(
                    namespace.repository_info.external_slug, namespace.sha, repository.head_sha
                )
                logger.info(
                    "Updating repo %s index with %d new files, %d changed files and %d delete files.",
                    repo_id,
                    len(new_files),
                    len(changed_files),
                    len(deleted_files),
                )

                if changed_files or deleted_files:
                    self.delete_sources(namespace.repository_info.external_slug, changed_files + deleted_files)

                loader_limit_paths_to = new_files + changed_files
            else:
                logger.info("Creating repo %s full index.", repo_id)

            logger.debug("Extracting chunks from repo %s", namespace.repository_info.external_slug)

            with self.repo_client.load_repo(namespace.repository_info.external_slug) as repo_dir:
                loader = GenericLanguageLoader.from_filesystem(
                    repo_dir,
                    limit_to=loader_limit_paths_to,
                    documents_metadata={"repo_id": namespace.repository_info.external_slug},
                    tokenizer_model=self.embedding.model,
                )
                documents = loader.load_and_split()
            logger.info("Indexing %d chunks from repo %s", len(documents), namespace.repository_info.external_slug)

            if documents:
                Chroma.from_documents(documents=documents, embedding=self.embedding, **self.db_common_kwargs())
        except:
            logger.error("Error indexing repo %s", namespace.repository_info.external_slug)
            namespace.status = CodebaseNamespace.Status.FAILED
            namespace.save(update_fields=["status", "modified"])
            raise
        else:
            namespace.status = CodebaseNamespace.Status.INDEXED
            namespace.save(update_fields=["status", "modified"])
            logger.info("Index finished for repo %s", namespace.repository_info.external_slug)

    def query(
        self,
        query: str,
        repo_id: str | None = None,
        content_type: Literal["functions_classes", "simplified_code"] | None = None,
        **kwargs,
    ) -> list[Document] | None:
        """
        Query the codebase.
        """
        conditions: list[dict[str, str]] = []
        if repo_id:
            conditions.append({"repo_id": repo_id})
        if content_type:
            conditions.append({"content_type": content_type})

        chroma_filter: dict[str, str | list] = {}
        if len(conditions) > 1:
            chroma_filter = {"$and": conditions}
        elif len(conditions) == 1:
            chroma_filter = conditions[0]

        results = self.db.similarity_search_with_relevance_scores(query, filter=chroma_filter, **kwargs)

        if not results:
            return None

        return [result[0] for result in results]

    def search_most_similar_filepath(self, repo_id: str, repository_file: RepositoryFile) -> str | None:
        """
        Search the most similar file path in the codebase.
        """
        documents = list(
            LanguageParser().lazy_parse(
                Blob.from_data(cast(str, repository_file.content), metadata={"source": repository_file.file_path})
            )
        )

        chunk_to_search = None
        if len(documents) == 1:
            chunk_to_search = documents[0].page_content
        else:
            for document in documents:
                if "language" in document.metadata and document.metadata["content_type"] == "simplified_code":
                    chunk_to_search = document.page_content
                    break

        if not chunk_to_search:
            return None

        result = self.query(chunk_to_search, repo_id=repo_id, k=1, score_threshold=0.6)

        if not result:
            # Fallback to try to find the file by the file path
            get_result = self.db.get(
                include=["metadatas"],
                where={"$and": [{"repo_id": repo_id}, {"source": repository_file.file_path}]},
                limit=1,
            )

            if get_result["metadatas"]:
                return get_result["metadatas"][0]["source"]
            return None

        return result[0].metadata["source"]

    def extract_tree(self, repo_id: str, ref: str | None = None) -> set[str]:
        """
        Extract the tree of a repository.
        """
        extracted_paths = set()
        with self.repo_client.load_repo(repo_id, sha=ref) as repo_dir:
            for dirpath, dirnames, filenames in repo_dir.walk():
                for dirname in dirnames:
                    extracted_paths.add(dirpath.joinpath(dirname).relative_to(repo_dir).as_posix())
                for filename in filenames:
                    extracted_paths.add(dirpath.joinpath(filename).relative_to(repo_dir).as_posix())
        return extracted_paths