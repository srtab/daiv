import logging

from codebase.context import set_runtime_ctx
from codebase.managers.issue_addressor import IssueAddressorManager
from core.utils import locked_task
from daiv import async_task

logger = logging.getLogger("daiv.tasks")


@async_task()
@locked_task(key="{repo_id}:{issue_iid}")
async def address_issue_task(repo_id: str, issue_iid: int, ref: str | None = None):
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        ref (str): The reference.
    """
    async with set_runtime_ctx(repo_id, ref=ref, scope="issue") as runtime_ctx:
        await IssueAddressorManager.address_issue(issue_iid=issue_iid, runtime_ctx=runtime_ctx)


@async_task()
@locked_task(key="{repo_id}:{merge_request_id}")
async def address_mr_review_task(repo_id: str, merge_request_id: int, merge_request_source_branch: str):
    """
    Address a review feedback by applying the changes described or answering questions about the codebase.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        merge_request_source_branch (str): The merge request source branch.
    """
    # async with set_runtime_ctx(repo_id, ref=merge_request_source_branch, scope="merge_request") as runtime_ctx:
    #     await ReviewAddressorManager.process_review_comments(merge_request_id=merge_request_id, runtime_ctx=runtime_ctx) # noqa: E501 ERA001


@async_task()
@locked_task(key="{repo_id}:{merge_request_id}")
async def address_mr_comments_task(repo_id: str, merge_request_id: int, merge_request_source_branch: str):
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        merge_request_source_branch (str): The merge request source branch.
    """
    # async with set_runtime_ctx(
    #     repo_id, ref=merge_request_source_branch, merge_request_id=merge_request_id, scope="merge_request" # noqa: E501 ERA001
    # ) as runtime_ctx:
    #     await ReviewAddressorManager.process_comments(merge_request_id=merge_request_id, runtime_ctx=runtime_ctx) # noqa: E501 ERA001
