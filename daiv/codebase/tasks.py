import logging

from django.core.cache import cache

from celery import shared_task

from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import ReviewAddressorManager

logger = logging.getLogger("daiv.tasks")


@shared_task
def update_index_by_repo_id(repo_ids: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given IDs.
    """
    for repo_id in repo_ids:
        update_index_repository(repo_id, reset)


@shared_task
def update_index_by_topics(topics: list[str], reset: bool = False):
    """
    Update the index of all repositories with the given topics.
    """
    repo_client = RepoClient.create_instance()
    for repository in repo_client.list_repositories(topics=topics, load_all=True):
        update_index_repository(repository.slug, reset)


@shared_task
def update_index_repository(repo_id: str, ref: str | None = None, reset: bool = False):
    """
    Update codebase index of a repository.
    """
    repo_client = RepoClient.create_instance()
    indexer = CodebaseIndex(repo_client=repo_client)
    if reset:
        indexer.delete(repo_id=repo_id, ref=ref)
    indexer.update(repo_id=repo_id, ref=ref)


@shared_task
def address_issue_task(
    repo_id: str,
    issue_iid: int,
    ref: str | None = None,
    should_reset_plan: bool = False,
    lock_cache_key: str | None = None,
):
    """
    Address an issue by creating a merge request with the changes described on the issue description.
    """
    try:
        IssueAddressorManager.process_issue(repo_id, issue_iid, ref, should_reset_plan)
    except Exception as e:
        logger.exception("Error addressing issue '%d': %s", issue_iid, e)
    finally:
        if lock_cache_key:
            # Delete the lock after the task is completed
            cache.delete(lock_cache_key)


@shared_task
def address_review_task(
    repo_id: str, merge_request_id: int, merge_request_source_branch: str, lock_cache_key: str | None = None
):
    try:
        ReviewAddressorManager.process_review(repo_id, merge_request_id, merge_request_source_branch)
    except Exception as e:
        logger.exception("Error addressing review of merge request '%d': %s", merge_request_id, e)
    finally:
        if lock_cache_key:
            # Delete the lock after the task is completed
            cache.delete(lock_cache_key)
