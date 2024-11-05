from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.db.models import Q

from langchain.retrievers import EnsembleRetriever

from codebase.document_loaders import GenericLanguageLoader
from codebase.models import CodebaseNamespace
from codebase.search_engines.lexical import LexicalSearchEngine
from codebase.search_engines.semantic import SemanticSearchEngine
from codebase.utils import analyze_repository
from core.config import RepositoryConfig

if TYPE_CHECKING:
    from langchain_core.documents import Document

    from codebase.clients import AllRepoClient

logger = logging.getLogger("daiv.indexes")

LEXICAL_INDEX_ENABLED = False


class CodebaseIndex(abc.ABC):
    """
    Abstract base class for managing codebase indexing operations.

    Handles creation, updates, deletion and searching of code indexes
    using both semantic and lexical search engines.

    Attributes:
        repo_client: Client for interacting with code repositories
    """

    repo_client: AllRepoClient

    def __init__(self, repo_client: AllRepoClient):
        self.repo_client = repo_client
        self.semantic_search_engine = SemanticSearchEngine()
        if LEXICAL_INDEX_ENABLED:
            self.lexical_search_engine = LexicalSearchEngine()

    def update(self, repo_id: str, ref: str | None = None):
        """
        Update or create the index of a repository.

        Args:
            repo_id (str): The repository identifier
            ref (str | None): The reference branch or tag. If None, uses default branch

        Note:
            For the default branch, performs full index update on first run.
            For other branches, only updates changed files.
        """
        repository = self.repo_client.get_repository(repo_id)
        repo_config = RepositoryConfig.get_config(repo_id, repository)
        ref = cast(str, ref or repo_config.default_branch)
        repo_head_sha = self.repo_client.get_repo_head_sha(repo_id, branch=ref)

        namespace, created = CodebaseNamespace.objects.get_or_create_from_repository(
            repository, tracking_ref=ref, head_sha=repo_head_sha
        )

        if not created and namespace.sha == repo_head_sha:
            logger.info("Repo %s index already updated.", repo_id)
            return

        namespace.status = CodebaseNamespace.Status.INDEXING
        namespace.save(update_fields=["status", "modified"])

        loader_limit_paths_to = []

        try:
            with transaction.atomic:
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
                        self._delete_documents(
                            namespace.repository_info.external_slug, ref, changed_files + deleted_files
                        )

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
                    self.semantic_search_engine.add_documents(namespace, documents)
                    if LEXICAL_INDEX_ENABLED:
                        self.lexical_search_engine.add_documents(namespace, documents)
        except Exception:
            logger.exception("Error indexing repo %s[%s]", namespace.repository_info.external_slug, ref)
            namespace.status = CodebaseNamespace.Status.FAILED
            namespace.save(update_fields=["status", "modified"])
        else:
            namespace.status = CodebaseNamespace.Status.INDEXED
            namespace.save(update_fields=["status", "modified"])
            logger.info("Index finished for repo %s[%s]", namespace.repository_info.external_slug, ref)

    def _delete_documents(self, repo_id: str, ref: str, source_files: list[str]):
        """
        Delete specific source files from both semantic and lexical indexes.

        Args:
            repo_id (str): The repository identifier
            ref (str): The reference branch or tag
            source_files (list[str]): List of file paths to delete from indexes
        """
        namespace = self._get_codebase_namespace(repo_id, ref)

        if namespace is None:
            return

        self.semantic_search_engine.delete_documents(namespace, source=source_files)
        if LEXICAL_INDEX_ENABLED:
            self.lexical_search_engine.delete_documents(repo_id, "page_source", source_files)

    def delete(self, repo_id: str, ref: str | None = None):
        """
        Delete all indexes for a repository.

        Args:
            repo_id (str): The repository identifier
            ref (str | None): The reference branch or tag. If None, uses default branch
        """
        namespace = self._get_codebase_namespace(repo_id, ref)

        if namespace is None:
            return

        logger.info("Reseting repo %s[%s] index.", repo_id, namespace.tracking_ref)

        self.semantic_search_engine.delete(namespace)
        if LEXICAL_INDEX_ENABLED:
            self.lexical_search_engine.delete(repo_id)

        namespace.delete()

    def search(self, repo_id: str, ref: str, query: str) -> list[Document]:
        """
        Search the repository index using semantic and lexical search.

        Args:
            repo_id (str): The repository identifier
            ref (str): The reference branch or tag
            query (str): The search query string

        Returns:
            list[Document]: List of matching documents from the search
        """
        namespace = self._get_codebase_namespace(repo_id, ref)

        if namespace is None:
            return []

        semantic_retriever = self.semantic_search_engine.as_retriever(
            namespace, k=10, metadata__contains={"content_type": "simplified_code"}
        )

        if LEXICAL_INDEX_ENABLED:
            return EnsembleRetriever(
                retrievers=[semantic_retriever, self.lexical_search_engine.as_retriever(repo_id, k=10)],
                weights=[0.6, 0.4],
            ).invoke(query)
        return semantic_retriever.invoke(query)

    def extract_tree(self, repo_id: str, ref: str) -> str:
        """
        Extract and return the file tree structure of a repository.

        Args:
            repo_id (str): The repository identifier
            ref (str): The reference branch or tag

        Returns:
            str: String representation of the repository's file tree
        """
        repo_config = RepositoryConfig.get_config(repo_id)

        with self.repo_client.load_repo(repo_id, sha=ref) as repo_dir:
            return analyze_repository(repo_dir, repo_config.combined_exclude_patterns)

    # TODO: Cache the namespace
    def _get_codebase_namespace(self, repo_id: str, ref: str | None) -> CodebaseNamespace | None:
        """
        Retrieve the CodebaseNamespace object for a given repository.

        Args:
            repo_id (str): The repository identifier
            ref (str | None): The reference branch or tag. If None, uses default branch

        Returns:
            CodebaseNamespace | None: The namespace object if found, None otherwise
        """
        repo_config = RepositoryConfig.get_config(repo_id)
        _ref = cast(str, ref or repo_config.default_branch)

        return CodebaseNamespace.objects.filter(
            Q(repository_info__external_slug=repo_id) | Q(repository_info__external_id=repo_id), tracking_ref=_ref
        ).first()
