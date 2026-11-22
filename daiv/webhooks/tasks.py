import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from django.conf import settings as django_settings
from django.core.management import call_command
from django.tasks import task

from crontask import cron
from sessions.inbox.kinds import SourceKey
from sessions.merge_request_runs.tasks import address_merge_request_task
from sessions.services import aget_session_ref, aget_task_run

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from webhooks.conf import settings

if TYPE_CHECKING:
    from django.tasks import TaskContext

    from sessions.executor.result import AgentResult


logger = logging.getLogger("daiv.tasks")


if codebase_settings.CLIENT == GitPlatform.GITLAB:

    @cron(settings.WEBHOOK_SETUP_CRON)
    @task
    def setup_webhooks_cron_task():
        """
        Setup webhooks for all repositories periodically.
        """
        call_command("setup_webhooks", disable_ssl_verification=django_settings.DEBUG)


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
    task_run = await aget_task_run(context.task_result.id)
    return await IssueAddressorManager.address_issue(
        repo_id=repo_id,
        issue=issue,
        mention_comment_id=mention_comment_id,
        mention_note_id=mention_note_id,
        ref=session_ref or None,
        thread_id=thread_id,
        sandbox_env_id=sandbox_environment_id,
        run_id=task_run.run_id,
        use_max=use_max,
        acting_user_id=task_run.user_id,
    )


# A label trigger whose run finished is a new request: DAIV's reaction, once removed, no longer blocks it.
address_issue_label_task = replace(address_issue_task, dedup_successful=False)


@task(takes_context=True)
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
    """One-release alias of :func:`sessions.merge_request_runs.tasks.address_merge_request_task`, so task rows queued
    under this path still run."""
    return await address_merge_request_task.func(
        context,
        repo_id=repo_id,
        merge_request_id=merge_request_id,
        thread_id=thread_id,
        use_max=use_max,
        request_key=SourceKey.note(mention_note_id),
        mention_comment_id=mention_comment_id,
        mention_note_id=mention_note_id,
        sandbox_environment_id=sandbox_environment_id,
    )
