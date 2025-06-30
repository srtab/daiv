import logging

from django.core.management import call_command

from asgiref.sync import async_to_sync
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
@locked_task(key="cleanup_indexes")
def cleanup_indexes_task(
    repo_id: str | None = None, check_accessibility: bool = True, cleanup_old_branches: bool = True
):
    """
    Clean up outdated indexes and inaccessible repositories.

    Args:
        repo_id (str | None): Limit cleanup to a specific repository by namespace, slug or id.
        check_accessibility (bool): Check repository accessibility and remove indexes for inaccessible repositories.
        cleanup_old_branches (bool): Clean up indexes from non-default branches older than the threshold.
    """
    call_command(
        "cleanup_indexes",
        check_accessibility=check_accessibility,
        cleanup_old_branches=cleanup_old_branches,
        repo_id=repo_id,
        no_input=True,
    )


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
    async_to_sync(IssueAddressorManager.plan_issue)(repo_id, issue_iid, ref, should_reset_plan)


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
    async_to_sync(ReviewAddressorManager.process_review)(repo_id, merge_request_id, ref=merge_request_source_branch)


@shared_task
@locked_task(key="{repo_id}:{merge_request_id}")
def fix_pipeline_job_task(repo_id: str, ref: str, merge_request_id: int, job_id: int, job_name: str):
    """
    Try to fix a failed pipeline of a merge request.

    Args:
        repo_id (str): The repository id.
        ref (str): The reference.
        merge_request_id (int): The merge request id.
        job_id (int): The job id.
        job_name (str): The job name.
    """
    async_to_sync(PipelineFixerManager.process_job)(repo_id, ref, merge_request_id, job_id, job_name)
