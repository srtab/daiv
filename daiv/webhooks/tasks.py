import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from django.conf import settings as django_settings
from django.core.management import call_command
from django.tasks import task

from crontask import cron
from sessions.services import aget_session_ref, aget_task_run_id

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from codebase.exceptions import CloneRefNotFoundError
from webhooks.conf import settings

if TYPE_CHECKING:
    from django.tasks import TaskContext

    from sessions.executor.result import AgentResult

    from codebase.base import MergeRequest

logger = logging.getLogger("daiv.tasks")


if codebase_settings.CLIENT == GitPlatform.GITLAB:

    @cron(settings.WEBHOOK_SETUP_CRON)
    @task
    def setup_webhooks_cron_task():
        """
        Setup webhooks for all repositories periodically.
        """
        call_command("setup_webhooks", disable_ssl_verification=django_settings.DEBUG)


def _mr_comment_skip_result(response: str, merge_request: MergeRequest) -> AgentResult:
    from sessions.executor.result import AgentResult

    return AgentResult(
        response=response,
        code_changes=False,
        merge_request_id=merge_request.merge_request_id,
        merge_request_web_url=merge_request.web_url,
        usage=None,
        question=None,
        publish=None,
    )


@task(dedup=True, dedup_on=("repo_id", "issue_iid", "mention_note_id"), takes_context=True)
async def address_issue_task(
    context: TaskContext,
    repo_id: str,
    issue_iid: int,
    thread_id: str,
    use_max: bool,
    mention_comment_id: str | None = None,
    mention_note_id: int | None = None,
    sandbox_environment_id: str | None = None,
) -> AgentResult:
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        context (TaskContext): Names the task result the webhook linked this run's ``Run`` row to.
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        thread_id (str): The LangGraph checkpoint key minted by the caller.
        use_max (bool): The max mode the callback decided and stamped on the ``Run``.
        mention_comment_id (str | None): The mention comment id. Defaults to None.
        mention_note_id (int | None): The note that mentioned DAIV, which the run answers; with ``repo_id`` and the
            iid, the run's dedup key, so each reply in one GitLab discussion gets its own run.
        sandbox_environment_id (str | None): The env the callback selected; ``None``: the GLOBAL default.
    """
    from webhooks.managers.issue_addressor import IssueAddressorManager

    client = RepoClient.create_instance()
    issue = client.get_issue(repo_id, issue_iid)
    # Unguarded on purpose: degrading a failed read to "" re-clones the default branch, which is the exact loss
    # ``Session.ref`` exists to prevent.
    session_ref = await aget_session_ref(thread_id=thread_id)
    return await IssueAddressorManager.address_issue(
        repo_id=repo_id,
        issue=issue,
        mention_comment_id=mention_comment_id,
        mention_note_id=mention_note_id,
        ref=session_ref or None,
        thread_id=thread_id,
        sandbox_env_id=sandbox_environment_id,
        run_id=await aget_task_run_id(context.task_result.id),
        use_max=use_max,
    )


# A label trigger whose run finished is a new request: DAIV's reaction, once removed, no longer blocks it.
address_issue_label_task = replace(address_issue_task, dedup_successful=False)


@task(dedup=True, dedup_on=("repo_id", "merge_request_id", "mention_note_id"), takes_context=True)
async def address_mr_comments_task(
    context: TaskContext,
    repo_id: str,
    merge_request_id: int,
    mention_comment_id: str,
    thread_id: str,
    use_max: bool,
    mention_note_id: int | None = None,
    sandbox_environment_id: str | None = None,
) -> AgentResult:
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        context (TaskContext): Names the task result the webhook linked this run's ``Run`` row to.
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        mention_comment_id (str): The mention comment id.
        thread_id (str): The LangGraph checkpoint key minted by the caller.
        use_max (bool): The max mode the callback decided and stamped on the ``Run``.
        mention_note_id (int | None): The note that mentioned DAIV, which the run answers; with ``repo_id`` and the
            iid, the run's dedup key, so each reply in one GitLab discussion gets its own run.
        sandbox_environment_id (str | None): The env the callback selected; ``None``: the GLOBAL default.
    """
    from webhooks.managers.review_addressor import CommentsAddressorManager

    client = RepoClient.create_instance()
    merge_request = client.get_merge_request(repo_id, merge_request_id)

    if merge_request.merged:
        response = (
            f"This merge request has already been merged, so I can't act on comments left against "
            f"its branch (`{merge_request.source_branch}`). Please open a new issue or merge request "
            f"for any follow-up changes."
        )
        logger.warning("Skipping MR-comment run for %s!%s: already merged.", repo_id, merge_request_id)
        client.create_merge_request_comment(repo_id, merge_request_id, body=response)
        return _mr_comment_skip_result(response, merge_request)

    try:
        return await CommentsAddressorManager.address_comments(
            repo_id=repo_id,
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            mention_note_id=mention_note_id,
            thread_id=thread_id,
            sandbox_env_id=sandbox_environment_id,
            run_id=await aget_task_run_id(context.task_result.id),
            use_max=use_max,
        )
    except CloneRefNotFoundError:
        response = (
            f"The source branch `{merge_request.source_branch}` for this merge request no longer "
            f"exists, so I can't check it out to address this comment. If it was merged and deleted, "
            f"please open a new issue or merge request for any follow-up changes."
        )
        logger.warning(
            "Skipping MR-comment run for %s!%s: source branch %r no longer exists.",
            repo_id,
            merge_request_id,
            merge_request.source_branch,
        )
        client.create_merge_request_comment(repo_id, merge_request_id, body=response)
        return _mr_comment_skip_result(response, merge_request)
