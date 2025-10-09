import logging

from asgiref.sync import async_to_sync
from celery import shared_task

from codebase.context import sync_set_repository_ctx
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import ReviewAddressorManager
from core.utils import locked_task

logger = logging.getLogger("daiv.tasks")


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
    with sync_set_repository_ctx(repo_id, ref=ref):
        async_to_sync(IssueAddressorManager.plan_issue)(repo_id, issue_iid, ref, should_reset_plan)


@shared_task
@locked_task(key="{repo_id}:{merge_request_id}")
def address_mr_review_task(repo_id: str, merge_request_id: int, merge_request_source_branch: str):
    """
    Address a review feedback by applying the changes described or answering questions about the codebase.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        merge_request_source_branch (str): The merge request source branch.
    """
    with sync_set_repository_ctx(repo_id, ref=merge_request_source_branch):
        async_to_sync(ReviewAddressorManager.process_review_comments)(
            repo_id, merge_request_id, ref=merge_request_source_branch
        )


@shared_task
@locked_task(key="{repo_id}:{merge_request_id}")
def address_mr_comments_task(repo_id: str, merge_request_id: int, merge_request_source_branch: str):
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        merge_request_source_branch (str): The merge request source branch.
    """
    with sync_set_repository_ctx(repo_id, ref=merge_request_source_branch):
        async_to_sync(ReviewAddressorManager.process_comments)(
            repo_id, merge_request_id, ref=merge_request_source_branch
        )
