import logging

from django.core.cache import cache
from django.core.management import call_command

from celery import shared_task

from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.pipeline_fixer import PipelineFixerManager
from codebase.managers.review_addressor import ReviewAddressorManager
from core.utils import locked_task

logger = logging.getLogger("daiv.tasks")


@shared_task
def update_index_repository(repo_id: str, ref: str | None = None, reset: bool = False):
    """
    Update codebase index of a repository.

    Args:
        repo_id (str): The repository id.
        ref (str): The reference.
        reset (bool): Whether to reset the index before updating.
    """
    call_command("update_index", repo_id=repo_id, ref=ref, reset=reset)


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
        logger.exception("Error addressing issue '%s[%s]:%d': %s", repo_id, ref, issue_iid, e)
    finally:
        if lock_cache_key:
            # Delete the lock after the task is completed
            cache.delete(lock_cache_key)


@shared_task
def address_review_task(
    repo_id: str, merge_request_id: int, merge_request_source_branch: str, lock_cache_key: str | None = None
):
    try:
        ReviewAddressorManager.process_review(repo_id, merge_request_id, ref=merge_request_source_branch)
    except Exception as e:
        logger.exception(
            "Error addressing review of merge request '%s[%s]:%d': %s",
            repo_id,
            merge_request_source_branch,
            merge_request_id,
            e,
        )
    finally:
        if lock_cache_key:
            # Delete the lock after the task is completed
            cache.delete(lock_cache_key)


@shared_task
@locked_task(key="{repo_id}:{ref}:{job_id}")
def fix_pipeline_job_task(repo_id: str, ref: str, merge_request_id: int, job_id: int, job_name: str):
    """
    Try to fix a failed pipeline of a merge request.
    """
    try:
        PipelineFixerManager.process_job(repo_id, ref, merge_request_id, job_id, job_name)
    except Exception as e:
        logger.exception("Error fixing pipeline job '%s[%s]:%d': %s", repo_id, ref, job_id, e)
