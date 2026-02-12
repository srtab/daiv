import logging

from django.conf import settings
from django.core.management import call_command

from crontask import cron
from django_tasks import task

from codebase.base import GitPlatform, Scope
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from codebase.context import set_runtime_ctx
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import CommentsAddressorManager

logger = logging.getLogger("daiv.tasks")


if codebase_settings.CLIENT == GitPlatform.GITLAB:

    @cron("*/5 * * * *")  # every 5 minute
    @task
    def setup_webhooks_cron_task():
        """
        Setup webhooks for all repositories every 5 minutes.
        """
        call_command("setup_webhooks", disable_ssl_verification=settings.DEBUG)  # noqa: S106


@task(dedup=True)
async def address_issue_task(
    repo_id: str, issue_iid: int, mention_comment_id: str | None = None, ref: str | None = None
):
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        mention_comment_id (str | None): The mention comment id. Defaults to None.
        ref (str | None): The reference. Defaults to None.
    """
    client = RepoClient.create_instance()
    issue = client.get_issue(repo_id, issue_iid)
    async with set_runtime_ctx(repo_id, scope=Scope.ISSUE, ref=ref, issue=issue) as runtime_ctx:
        await IssueAddressorManager.address_issue(
            issue=issue, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx
        )


@task(dedup=True)
async def address_mr_comments_task(repo_id: str, merge_request_id: int, mention_comment_id: str):
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        mention_comment_id (str): The mention comment id.
    """
    client = RepoClient.create_instance()
    merge_request = client.get_merge_request(repo_id, merge_request_id)
    async with set_runtime_ctx(
        repo_id, scope=Scope.MERGE_REQUEST, ref=merge_request.source_branch, merge_request=merge_request
    ) as runtime_ctx:
        await CommentsAddressorManager.address_comments(
            merge_request=merge_request, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx
        )
