from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.db.models import Q

from langchain.retrievers import EnsembleRetriever
from langchain_community.document_loaders.blob_loaders import Blob
from langchain_community.document_loaders.parsers.language import LanguageParser

from codebase.conf import settings
from codebase.document_loaders import GenericLanguageLoader
from codebase.models import CodebaseNamespace
from codebase.search_engines.lexical import LexicalSearchEngine
from codebase.search_engines.semantic import SemanticSearchEngine
from core.config import RepositoryConfig

if TYPE_CHECKING:
    from langchain_core.documents import Document

    from codebase.base import RepositoryFile
    from codebase.clients import AllRepoClient

logger = logging.getLogger(__name__)

LEXICAL_INDEX_ENABLED = False


class CodebaseIndex(abc.ABC):
    repo_client: AllRepoClient

    def __init__(self, repo_client: AllRepoClient):
        self.repo_client = repo_client
        self.semantic_search_engine = SemanticSearchEngine(collection_name=settings.CODEBASE_COLLECTION_NAME)
        if LEXICAL_INDEX_ENABLED:
            self.lexical_search_engine = LexicalSearchEngine()

    @transaction.atomic
    def update(self, repo_id: str, ref: str | None = None):
        """
        Update the index of a repository.
        """
        repository = self.repo_client.get_repository(repo_id)
        repo_config = RepositoryConfig.get_config(repo_id, repository)
        ref = cast(str, ref or repo_config.default_branch)
        repo_head_sha = self.repo_client.get_repo_head_sha(repo_id, branch=ref)

        namespace, created = CodebaseNamespace.objects.get_or_create_from_repository(repository, tracking_ref=ref)

        if not created and namespace.sha == repo_head_sha:
            logger.info("Repo %s index already updated.", repo_id)
            return

        namespace.status = CodebaseNamespace.Status.INDEXING
        namespace.save(update_fields=["status", "modified"])

        try:
            loader_limit_paths_to = []

            # For the default branch, the index is fully updated on the first run, otherwise,
            # For other branches, the index is updated only with changed files.
            if not created and namespace.sha != repo_head_sha:
                new_files, changed_files, deleted_files = self.repo_client.get_commit_changed_files(
                    namespace.repository_info.external_slug, namespace.sha, repo_head_sha
                )
                logger.info(
                    "Updating repo %s[%s] index with %d new files, %d changed files and %d delete files.",
                    repo_id,
                    ref,
                    len(new_files),
                    len(changed_files),
                    len(deleted_files),
                )

                if changed_files or deleted_files:
                    self._delete_documents(namespace.repository_info.external_slug, ref, changed_files + deleted_files)

                loader_limit_paths_to = new_files + changed_files
            else:
                logger.info("Creating repo %s[%s] full index.", repo_id, ref)

            logger.debug("Extracting chunks from repo %s[%s]", namespace.repository_info.external_slug, ref)

            with self.repo_client.load_repo(namespace.repository_info.external_slug, ref) as repo_dir:
                loader = GenericLanguageLoader.from_filesystem(
                    repo_dir,
                    limit_to=loader_limit_paths_to,
                    exclude=repo_config.combined_exclude_patterns,
                    documents_metadata={"repo_id": namespace.repository_info.external_slug, "ref": ref},
                )
                documents = loader.load_and_split()
            logger.info(
                "Indexing %d chunks from repo %s[%s]", len(documents), namespace.repository_info.external_slug, ref
            )

            if documents:
                self.semantic_search_engine.add_documents(repo_id, documents)
                if LEXICAL_INDEX_ENABLED:
                    self.lexical_search_engine.add_documents(repo_id, documents)
        except:
            logger.error("Error indexing repo %s[%s]", namespace.repository_info.external_slug, ref)
            namespace.status = CodebaseNamespace.Status.FAILED
            namespace.save(update_fields=["status", "modified"])
            raise
        else:
            namespace.status = CodebaseNamespace.Status.INDEXED
            namespace.save(update_fields=["status", "modified"])
            logger.info("Index finished for repo %s[%s]", namespace.repository_info.external_slug, ref)

    def _delete_documents(self, repo_id: str, ref: str, source_files: list[str]):
        """
        Delete source files from the indexes.
        """
        self.semantic_search_engine.delete_documents(repo_id, ref, "source", source_files)
        if LEXICAL_INDEX_ENABLED:
            self.lexical_search_engine.delete_documents(repo_id, "page_source", source_files)

    def delete(self, repo_id: str, ref: str | None = None):
        """
        Delete a repository indexes.
        """
        logger.info("Reseting repo %s[%s] index.", repo_id, ref)

        repo_config = RepositoryConfig.get_config(repo_id)
        _ref = cast(str, ref or repo_config.default_branch)

        self.semantic_search_engine.delete(repo_id)
        if LEXICAL_INDEX_ENABLED:
            self.lexical_search_engine.delete(repo_id)

        CodebaseNamespace.objects.filter(
            Q(repository_info__external_slug=repo_id) | Q(repository_info__external_id=repo_id), tracking_ref=_ref
        ).delete()

    def search(self, repo_id: str, ref: str, query: str) -> list[Document]:
        """
        Search the repository index.

        Args:
            repo_id (str): The repository id.
            ref (str): The repository reference.
            query (str): The query.

        Returns:
            list[Document]: The search results.
        """
        semantic_retriever = self.semantic_search_engine.as_retriever(
            repo_id, k=10, ref=ref, exclude_content_type="simplified_code"
        )
        if LEXICAL_INDEX_ENABLED:
            return EnsembleRetriever(
                retrievers=[semantic_retriever, self.lexical_search_engine.as_retriever(repo_id, k=10)],
                weights=[0.6, 0.4],
            ).invoke(query)
        return semantic_retriever.invoke(query)

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

    def extract_tree(self, repo_id: str, ref: str) -> set[str]:
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
