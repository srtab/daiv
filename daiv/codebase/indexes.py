from __future__ import annotations

import abc
import functools
import logging
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.db.models import Q, QuerySet

from gitlab import GitlabGetError
from langchain.retrievers import EnsembleRetriever

from codebase.document_loaders import GenericLanguageLoader
from codebase.models import CodebaseNamespace
from codebase.search_engines.lexical import LexicalSearchEngine
from codebase.search_engines.semantic import SemanticSearchEngine
from codebase.utils import analyze_repository
from core.config import RepositoryConfig

if TYPE_CHECKING:
    from langchain_core.retrievers import BaseRetriever

    from codebase.clients import AllRepoClient

logger = logging.getLogger("daiv.indexes")


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
        ref = cast("str", ref or repo_config.default_branch)

        try:
            repo_head_sha = self.repo_client.get_repo_head_sha(repo_id, branch=ref)
        except GitlabGetError as e:
            # If the branch is not found, it means that the branch has been deleted in the meantime,
            # so we skip the index update.
            if e.response_code == 404:
                logger.warning("Branch '%s' for repo '%s' not found, skipping index update.", ref, repo_id)
                return
            raise

        namespace, created = CodebaseNamespace.objects.get_or_create_from_repository(
            repository, tracking_ref=ref, head_sha=repo_head_sha
        )

        if not created and namespace.sha == repo_head_sha:
            logger.info("Repo %s[%s] index already updated.", repo_id, ref)
            return

        namespace.status = CodebaseNamespace.Status.INDEXING
        namespace.save(update_fields=["status", "modified"])

        loader_limit_paths_to = []

        try:
            with transaction.atomic():
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
                        documents_metadata={
                            "repo_id": namespace.repository_info.external_slug,
                            "ref": ref,
                            "default_branch": cast("str", repo_config.default_branch),
                        },
                    )
                    documents = loader.load_and_split()

                logger.info(
                    "Indexing %d chunks from repo %s[%s]", len(documents), namespace.repository_info.external_slug, ref
                )

                if documents:
                    self.semantic_search_engine.add_documents(namespace, documents)
                    self.lexical_search_engine.add_documents(namespace, documents)
        except Exception:
            logger.exception("Error indexing repo %s[%s]", namespace.repository_info.external_slug, ref)
            namespace.status = CodebaseNamespace.Status.FAILED
            namespace.save(update_fields=["status", "modified"])
        else:
            namespace.status = CodebaseNamespace.Status.INDEXED
            namespace.save(update_fields=["status", "modified"])
            logger.info("Index finished for repo %s[%s]", namespace.repository_info.external_slug, ref)
        finally:
            # Clear the cache for the namespace retrieval as it might have changed
            self._get_codebase_namespace.cache_clear()

    def _delete_documents(self, repo_id: str, ref: str, source_files: list[str]):
        """
        Delete specific source files from both semantic and lexical indexes.

        Args:
            repo_id (str): The repository identifier
            ref (str): The reference branch or tag
            source_files (list[str]): List of file paths to delete from indexes
        """
        namespace = self._get_codebase_namespace(repo_id, ref).first()

        if namespace is None:
            return

        self.semantic_search_engine.delete_documents(namespace, source=source_files)
        self.lexical_search_engine.delete_documents(namespace, source=source_files)

    def delete(self, repo_id: str, ref: str | None = None, delete_all: bool = False):
        """
        Delete indexes for a repository.

        Args:
            repo_id (str): The repository identifier
            ref (str | None): The reference branch or tag. If None, uses default branch
            delete_all (bool): If True, deletes all indexes for the repository
        """

        namespaces = self._get_codebase_namespace(repo_id, ref, ignore_ref=delete_all)

        if not namespaces.exists():
            return

        for namespace in namespaces.iterator():
            logger.info("Reseting repo %s[%s] index.", repo_id, namespace.tracking_ref)

            self.semantic_search_engine.delete(namespace)
            self.lexical_search_engine.delete(namespace)

            namespace.delete()

    def as_retriever(self, repo_id: str | None = None, ref: str | None = None) -> BaseRetriever:
        """
        Get a retriever for the repository index.

        Args:
            repo_id (str | None): The repository identifier
            ref (str | None): The reference branch or tag

        Returns:
            BaseRetriever: The retriever for the repository index
        """
        namespace = self._get_codebase_namespace(repo_id, ref).first() if repo_id else None

        if repo_id and namespace is None:
            raise ValueError(f"No namespace found for repo {repo_id} and ref {ref}.")

        return EnsembleRetriever(
            retrievers=[
                self.semantic_search_engine.as_retriever(namespace, k=10),
                self.lexical_search_engine.as_retriever(namespace, k=10),
            ],
            weights=[0.6, 0.4],
        )

    @functools.lru_cache(maxsize=32)  # noqa: B019
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

    @functools.lru_cache(maxsize=32)  # noqa: B019
    def _get_codebase_namespace(
        self, repo_id: str, ref: str | None, ignore_ref: bool = False
    ) -> QuerySet[CodebaseNamespace]:
        """
        Retrieve the CodebaseNamespace object for a given repository.

        Args:
            repo_id (str): The repository identifier
            ref (str | None): The reference branch or tag. If None, uses default branch
            ignore_ref (bool): If True, ignores the reference branch

        Returns:
            CodebaseNamespace | None: The namespace object if found, None otherwise
        """
        qs = CodebaseNamespace.objects.filter(
            Q(repository_info__external_slug=repo_id) | Q(repository_info__external_id=repo_id)
        )

        if ignore_ref:
            return qs

        repo_config = RepositoryConfig.get_config(repo_id)
        _ref = cast("str", ref or repo_config.default_branch)

        return qs.filter(tracking_ref=_ref)
