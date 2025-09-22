import logging

from asgiref.sync import async_to_sync
from celery import shared_task

from codebase.base import ClientType
from codebase.clients import RepoClient
from codebase.context import sync_set_repository_ctx
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import ReviewAddressorManager
from core.utils import locked_task

logger = logging.getLogger("daiv.tasks")


@shared_task
@locked_task(key="{repo_id}:{issue_iid}")
def address_issue_task(
    repo_id: str,
    issue_iid: int,
    client_type: ClientType,
    client_kwargs: dict | None = None,
    ref: str | None = None,
    should_reset_plan: bool = False,
):
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        client_type (ClientType): The client type.
        client_kwargs (dict): The client kwargs.
        ref (str): The reference branch or tag.
        should_reset_plan (bool): Whether to reset the plan before creating the merge request.
    """
    client_kwargs = client_kwargs or {}

    with sync_set_repository_ctx(repo_id, ref=ref, client=RepoClient.create_instance(client_type, **client_kwargs)):
        async_to_sync(IssueAddressorManager.plan_issue)(issue_iid, should_reset_plan)


@shared_task
@locked_task(key="{repo_id}:{merge_request_id}")
def address_review_task(
    repo_id: str,
    merge_request_id: int,
    merge_request_source_branch: str,
    client_type: ClientType,
    client_kwargs: dict | None = None,
):
    """
    Address a review feedback by applying the changes described or answering questions about the codebase.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        merge_request_source_branch (str): The merge request source branch.
    """
    client_kwargs = client_kwargs or {}

    with sync_set_repository_ctx(
        repo_id, ref=merge_request_source_branch, client=RepoClient.create_instance(client_type, **client_kwargs)
    ):
        async_to_sync(ReviewAddressorManager.process_review)(merge_request_id)
