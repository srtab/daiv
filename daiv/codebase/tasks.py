import logging

from django.tasks import task

from codebase.clients import RepoClient
from codebase.context import set_runtime_ctx
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import CommentsAddressorManager
from core.utils import locked_task

logger = logging.getLogger("daiv.tasks")


@task
@locked_task(key="{repo_id}:{issue_iid}")
async def address_issue_task(repo_id: str, issue_iid: int, mention_comment_id: str, ref: str | None = None):
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        mention_comment_id (str): The mention comment id.
        ref (str | None): The reference.
    """
    client = RepoClient.create_instance()
    issue = client.get_issue(repo_id, issue_iid)
    async with set_runtime_ctx(repo_id, ref=ref, scope="issue", issue=issue) as runtime_ctx:
        await IssueAddressorManager.address_issue(
            issue=issue, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx
        )


@task
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


@task
@locked_task(key="{repo_id}:{merge_request_id}")
async def address_mr_comments_task(
    repo_id: str, merge_request_id: int, merge_request_source_branch: str, mention_comment_id: str
):
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        merge_request_source_branch (str): The merge request source branch.
        mention_comment_id (str): The mention comment id.
    """
    client = RepoClient.create_instance()
    merge_request = client.get_merge_request(repo_id, merge_request_id)
    async with set_runtime_ctx(
        repo_id, ref=merge_request_source_branch, scope="merge_request", merge_request=merge_request
    ) as runtime_ctx:
        await CommentsAddressorManager.address_comments(
            merge_request=merge_request, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx
        )
