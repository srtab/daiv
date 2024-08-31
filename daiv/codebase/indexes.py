from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.db.models import Q

from langchain_community.document_loaders.blob_loaders import Blob
from langchain_community.document_loaders.parsers.language import LanguageParser

from codebase.conf import settings
from codebase.document_loaders import GenericLanguageLoader
from codebase.models import CodebaseNamespace
from codebase.reranker import RerankerEngine
from codebase.search_engines.lexical import LexicalSearchEngine
from codebase.search_engines.semantic import SemanticSearchEngine

if TYPE_CHECKING:
    from codebase.base import RepositoryFile
    from codebase.clients import GitHubClient, GitLabClient
    from codebase.search_engines.base import ScoredResult

logger = logging.getLogger(__name__)


class CodebaseIndex(abc.ABC):
    repo_client: GitLabClient | GitHubClient

    def __init__(self, repo_client: GitLabClient | GitHubClient):
        self.repo_client = repo_client
        self.semantic_search_engine = SemanticSearchEngine(collection_name=settings.CODEBASE_COLLECTION_NAME)
        self.lexical_search_engine = LexicalSearchEngine()

    @transaction.atomic
    def update(self, repo_id: str):
        """
        Update the index of a repository.
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
                    self.delete_documents(namespace.repository_info.external_slug, changed_files + deleted_files)

                loader_limit_paths_to = new_files + changed_files
            else:
                logger.info("Creating repo %s full index.", repo_id)

            logger.debug("Extracting chunks from repo %s", namespace.repository_info.external_slug)

            with self.repo_client.load_repo(namespace.repository_info.external_slug) as repo_dir:
                loader = GenericLanguageLoader.from_filesystem(
                    repo_dir,
                    limit_to=loader_limit_paths_to,
                    documents_metadata={"repo_id": namespace.repository_info.external_slug},
                    tokenizer_model=self.semantic_search_engine.embedding.model,
                )
                documents = loader.load_and_split()
            logger.info("Indexing %d chunks from repo %s", len(documents), namespace.repository_info.external_slug)

            if documents:
                self.semantic_search_engine.add_documents(repo_id, documents)
                self.lexical_search_engine.add_documents(repo_id, documents)
        except:
            logger.error("Error indexing repo %s", namespace.repository_info.external_slug)
            namespace.status = CodebaseNamespace.Status.FAILED
            namespace.save(update_fields=["status", "modified"])
            raise
        else:
            namespace.status = CodebaseNamespace.Status.INDEXED
            namespace.save(update_fields=["status", "modified"])
            logger.info("Index finished for repo %s", namespace.repository_info.external_slug)

    def delete_documents(self, repo_id: str, source_files: list[str]):
        """
        Delete source files from the indexes.
        """
        self.semantic_search_engine.delete_documents(repo_id, "source", source_files)
        self.lexical_search_engine.delete_documents(repo_id, "page_source", source_files)

    def delete(self, repo_id: str):
        """
        Delete a repository indexes.
        """
        logger.info("Reseting repo %s index.", repo_id)

        self.semantic_search_engine.delete(repo_id)
        self.lexical_search_engine.delete(repo_id)

        CodebaseNamespace.objects.filter(
            Q(repository_info__external_slug=repo_id) | Q(repository_info__external_id=repo_id)
        ).delete()

    def search_with_reranker(self, repo_id: str, query: str, k=10) -> list[tuple[float, ScoredResult]]:
        """
        Search the codebase and rerank the results.
        """
        semantic_results = self.semantic_search_engine.search(repo_id, query, k=k, content_type="functions_classes")
        lexical_results = self.lexical_search_engine.search(repo_id, query, k=k)
        combined_results = semantic_results + lexical_results
        if not combined_results:
            return []
        score_results = RerankerEngine.rerank(query, [result.document.page_content for result in combined_results])
        return [
            item
            for item in sorted(
                zip(score_results, combined_results, strict=True), key=lambda result: result[0], reverse=True
            )[:k]
            if item[0] > 0
        ]

    def search_most_similar_file(self, repo_id: str, repository_file: RepositoryFile) -> str | None:
        """
        Search the most similar file in the codebase.

        Args:
            repo_id (str): The repository id.
            repository_file (RepositoryFile): The file to search.

        Returns:
            str | None: The most similar file path or None if not found.
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

        if result := self.semantic_search_engine.search(repo_id, chunk_to_search, k=1, score_threshold=0.6):
            return result[0].document.metadata["source"]

        # Fallback to try to find the file by the file path
        if documents := self.semantic_search_engine.get_documents(repo_id, "source", repository_file.file_path):
            return documents[0].metadata["source"]
        return None

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
