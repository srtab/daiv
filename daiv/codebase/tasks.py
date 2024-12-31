import logging

from django.core.management import call_command

from celery import shared_task

from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.pipeline_fixer import PipelineFixerManager
from codebase.managers.review_addressor import ReviewAddressorManager
from core.utils import locked_task

logger = logging.getLogger("daiv.tasks")


@shared_task
@locked_task(key="{repo_id}:{ref}")
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
@locked_task(key="{repo_id}:{issue_iid}")
def address_issue_task(repo_id: str, issue_iid: int, ref: str | None = None, should_reset_plan: bool = False):
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        ref (str): The reference.
        should_reset_plan (bool): Whether to reset the plan before creating the merge request.
    """
    IssueAddressorManager.process_issue(repo_id, issue_iid, ref, should_reset_plan)


@shared_task
@locked_task(key="{repo_id}:{merge_request_id}")
def address_review_task(repo_id: str, merge_request_id: int, merge_request_source_branch: str):
    """
    Address a review feedback by applying the changes described or answering questions about the codebase.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        merge_request_source_branch (str): The merge request source branch.
    """
    try:
        ReviewAddressorManager.process_review(repo_id, merge_request_id, ref=merge_request_source_branch)
    except Exception:
        logger.exception(
            "Error addressing review of merge request '%s[%s]:%d'.",
            repo_id,
            merge_request_source_branch,
            merge_request_id,
        )


@shared_task
@locked_task(key="{repo_id}:{merge_request_id}:{job_name}")
def fix_pipeline_job_task(repo_id: str, ref: str, merge_request_id: int, job_id: int, job_name: str, thread_id: str):
    """
    Try to fix a failed pipeline of a merge request.

    Args:
        repo_id (str): The repository id.
        ref (str): The reference.
        merge_request_id (int): The merge request id.
        job_id (int): The job id.
        job_name (str): The job name.
        thread_id (str): The thread id.
    """
    try:
        PipelineFixerManager.process_job(repo_id, ref, merge_request_id, job_id, job_name, thread_id)
    except Exception:
        logger.exception("Error fixing pipeline job '%s[%d]:%d'.", repo_id, merge_request_id, job_id)
