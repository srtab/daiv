import logging
from datetime import UTC
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management import call_command

from crontask import cron
from django_tasks import task
from github.GithubException import GithubException
from gitlab.exceptions import GitlabError

from codebase.base import GitPlatform, Scope
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from codebase.context import set_runtime_ctx
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.managers.review_addressor import CommentsAddressorManager

if TYPE_CHECKING:
    from automation.agent.results import AgentResult

logger = logging.getLogger("daiv.tasks")


if codebase_settings.CLIENT == GitPlatform.GITLAB:

    @cron(codebase_settings.WEBHOOK_SETUP_CRON)
    @task
    def setup_webhooks_cron_task():
        """
        Setup webhooks for all repositories periodically.
        """
        call_command("setup_webhooks", disable_ssl_verification=settings.DEBUG)  # noqa: S106


@task(dedup=True)
async def address_issue_task(
    repo_id: str,
    issue_iid: int,
    mention_comment_id: str | None = None,
    ref: str | None = None,
    thread_id: str | None = None,
) -> AgentResult:
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        mention_comment_id (str | None): The mention comment id. Defaults to None.
        ref (str | None): The reference. Defaults to None.
        thread_id (str | None): The LangGraph checkpoint key minted by the caller. When ``None``
            the addressor recomputes it from the runtime context.
    """
    client = RepoClient.create_instance()
    issue = client.get_issue(repo_id, issue_iid)
    async with set_runtime_ctx(repo_id, scope=Scope.ISSUE, ref=ref, issue=issue) as runtime_ctx:
        return await IssueAddressorManager.address_issue(
            issue=issue, mention_comment_id=mention_comment_id, runtime_ctx=runtime_ctx, thread_id=thread_id
        )


@task(dedup=True)
async def record_merge_metrics_task(
    repo_id: str,
    merge_request_iid: int,
    title: str,
    source_branch: str,
    target_branch: str,
    merged_at: str,
    platform: str,
) -> dict[str, bool]:
    """
    Record merge metrics for a merged MR/PR with commit-based DAIV/human attribution.

    Fetches MR-level diff stats and the pre-squash commit list from the platform API.
    Each commit's author email is compared against the bot's commit email to determine
    attribution. If fetching fails, the metric is still recorded with zero counts.

    Args:
        repo_id: The repository ID.
        merge_request_iid: The merge request IID (GitLab) or number (GitHub).
        title: The merge request title.
        source_branch: The source branch.
        target_branch: The target branch.
        merged_at: The merge timestamp as an ISO datetime string, or empty string to use the current time.
        platform: The git platform ("gitlab" or "github").

    Returns:
        A dict with key "recorded" set to True on success.
    """
    from datetime import datetime

    from codebase.models import MergeMetric

    client = RepoClient.create_instance()

    # Fetch MR-level diff stats
    lines_added = 0
    lines_removed = 0
    files_changed = 0
    diff_stats_failed = False
    try:
        diff_stats = client.get_merge_request_diff_stats(repo_id, merge_request_iid)
        lines_added = diff_stats.lines_added
        lines_removed = diff_stats.lines_removed
        files_changed = diff_stats.files_changed
    except GitlabError, GithubException, OSError:
        diff_stats_failed = True
        logger.exception("Failed to fetch diff stats for %s!%d on %s", repo_id, merge_request_iid, platform)

    # Resolve bot email (separate from commit fetching so a failure here
    # doesn't discard successfully fetched commit data)
    bot_email: str | None = None
    try:
        bot_email = client.get_bot_commit_email()
    except GitlabError, GithubException, OSError:
        logger.exception("Failed to resolve bot email for %s on %s", repo_id, platform)

    # Fetch pre-squash commits for attribution
    daiv_lines_added = 0
    daiv_lines_removed = 0
    human_lines_added = 0
    human_lines_removed = 0
    total_commits = 0
    daiv_commits = 0

    try:
        commits = client.get_merge_request_commits(repo_id, merge_request_iid)
        total_commits = len(commits)
        for commit in commits:
            is_bot = bot_email is not None and commit.author_email.lower() == bot_email.lower()
            if is_bot:
                daiv_commits += 1
                daiv_lines_added += commit.lines_added
                daiv_lines_removed += commit.lines_removed
            else:
                human_lines_added += commit.lines_added
                human_lines_removed += commit.lines_removed

        # If diff stats API failed but commits succeeded, use commit sums as totals
        if diff_stats_failed:
            lines_added = daiv_lines_added + human_lines_added
            lines_removed = daiv_lines_removed + human_lines_removed
    except GitlabError, GithubException, OSError:
        logger.exception("Failed to fetch commits for %s!%d on %s", repo_id, merge_request_iid, platform)

    # Parse merged_at timestamp
    if merged_at:
        try:
            merged_at_dt = datetime.fromisoformat(merged_at)
        except ValueError:
            logger.warning(
                "Invalid merged_at timestamp '%s' for %s!%d on %s, using current time",
                merged_at,
                repo_id,
                merge_request_iid,
                platform,
            )
            merged_at_dt = datetime.now(tz=UTC)
    else:
        logger.warning(
            "No merged_at timestamp for %s!%d on %s, using current time", repo_id, merge_request_iid, platform
        )
        merged_at_dt = datetime.now(tz=UTC)

    await MergeMetric.objects.aupdate_or_create(
        repo_id=repo_id,
        merge_request_iid=merge_request_iid,
        platform=platform,
        defaults={
            "title": title,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "files_changed": files_changed,
            "daiv_lines_added": daiv_lines_added,
            "daiv_lines_removed": daiv_lines_removed,
            "human_lines_added": human_lines_added,
            "human_lines_removed": human_lines_removed,
            "total_commits": total_commits,
            "daiv_commits": daiv_commits,
            "merged_at": merged_at_dt,
            "target_branch": target_branch,
            "source_branch": source_branch,
        },
    )

    return {"recorded": True}


@task(dedup=True)
async def address_mr_comments_task(
    repo_id: str, merge_request_id: int, mention_comment_id: str, thread_id: str | None = None
) -> AgentResult:
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        mention_comment_id (str): The mention comment id.
        thread_id (str | None): The LangGraph checkpoint key minted by the caller. When ``None``
            the addressor recomputes it from the runtime context.
    """
    client = RepoClient.create_instance()
    merge_request = client.get_merge_request(repo_id, merge_request_id)
    async with set_runtime_ctx(
        repo_id, scope=Scope.MERGE_REQUEST, ref=merge_request.source_branch, merge_request=merge_request
    ) as runtime_ctx:
        return await CommentsAddressorManager.address_comments(
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            runtime_ctx=runtime_ctx,
            thread_id=thread_id,
        )
