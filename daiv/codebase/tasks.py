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
from core.utils import locked_task

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


@cron(codebase_settings.REPO_ACCESS_SYNC_CRON)
@task
@locked_task(key="repo-access")
def sync_repository_access_cron_task():
    """
    Mirror per-user repository access levels from the git platform into ``RepositoryAccess``.

    Repo-centric: one member-list call per bot-visible repository covers all users at once.
    A per-repo failure keeps that repo's previous rows (serve-stale) and leaves their
    ``synced_at`` untouched, so authorization expires them per-repo once they age past the
    hard TTL — a repo whose sync keeps failing eventually denies access to that repo alone,
    without affecting repos that are still syncing cleanly. Rows aged past the hard TTL are
    pruned each run: they already grant no access, and clearing them lets a repo that
    legitimately dropped to zero members stop tripping the empty-member guard once its stale
    rows expire. ``last_success_at`` advances only on a fully clean run and is used purely for
    observability, not the access decision (which is per-row) nor the backstop enqueue (keyed
    on ``last_started_at``).
    """
    from django.db import transaction
    from django.utils import timezone

    from codebase.models import RepositoryAccess, RepositoryAccessSyncState, RepositoryCatalog

    if codebase_settings.CLIENT == GitPlatform.SWE:
        return

    provider = codebase_settings.CLIENT.value
    state, _ = RepositoryAccessSyncState.objects.get_or_create(pk=RepositoryAccessSyncState.SINGLETON_PK)
    state.last_started_at = timezone.now()
    state.save(update_fields=["last_started_at"])

    client = RepoClient.create_instance()
    try:
        universe = client.list_repositories()
    except Exception:
        logger.exception("Repository access sync: failed to list repositories")
        state.status = RepositoryAccessSyncState.Status.FAILED
        state.save(update_fields=["status"])
        return

    # Snapshot which repos currently have rows, once, so the empty-member guard below does not
    # issue a per-repo EXISTS query across the whole universe every run.
    repos_with_rows = set(
        RepositoryAccess.objects.filter(provider=provider).values_list("repo_id", flat=True).distinct()
    )

    # Mirror the repository catalog (metadata + admin-visible universe) from the same universe
    # listing. Repo listings are served from this table instead of live platform fetches. An
    # empty universe yields an empty bulk_create (no-op); the prune below is guarded on it.
    catalog_synced_at = timezone.now()
    RepositoryCatalog.objects.bulk_create(
        [
            RepositoryCatalog(
                provider=provider,
                slug=repo.slug,
                name=repo.name,
                default_branch=repo.default_branch or "",
                html_url=repo.html_url,
                topics=repo.topics,
                synced_at=catalog_synced_at,
            )
            for repo in universe
        ],
        update_conflicts=True,
        unique_fields=["provider", "slug"],
        update_fields=["name", "default_branch", "html_url", "topics", "synced_at"],
    )

    failures = 0
    for repo in universe:
        try:
            members = client.list_repository_members(repo.slug)
            # An empty member list is almost always a degraded/partial API response (paginated
            # endpoints can return an empty first page without raising) rather than a genuine
            # membership wipe. For a repo that previously had rows, treat it as a failure and keep
            # those rows (serve-stale) instead of silently locking everyone out. A first-ever sync
            # has no rows to preserve, but an empty result is still suspicious, so log it — a
            # genuinely degraded first sync must be visible in logs, not indistinguishable from a
            # legitimately member-less repo silently recorded with zero rows.
            if not members:
                if repo.slug in repos_with_rows:
                    failures += 1
                    logger.warning(
                        "Repository access sync: %s returned no members but had prior rows; keeping previous rows",
                        repo.slug,
                    )
                else:
                    logger.warning("Repository access sync: %s returned no members on first sync", repo.slug)
                continue
            synced_at = timezone.now()
            rows = [
                RepositoryAccess(
                    provider=provider,
                    uid=member.uid,
                    username=member.username,
                    repo_id=repo.slug,
                    access_level=member.access_level,
                    synced_at=synced_at,
                )
                for member in members
            ]
            with transaction.atomic():
                RepositoryAccess.objects.filter(provider=provider, repo_id=repo.slug).delete()
                RepositoryAccess.objects.bulk_create(rows)
        except Exception:
            failures += 1
            logger.exception("Repository access sync: failed to sync %s (keeping previous rows)", repo.slug)
            continue

    # Prune access rows for repos no longer in the universe, so a repo the bot lost access to
    # (or that was deleted/renamed) is revoked promptly rather than only after the hard TTL.
    # This trusts the listing to be complete: a *fully empty* listing is treated as a degraded
    # response and the prune is skipped (see below), but a *partially truncated* listing —
    # non-empty yet missing repos — will still prune the missing repos' rows. That is an
    # accepted, self-healing fail-CLOSED event (users of dropped repos are denied until the next
    # complete sync recreates the rows ~1 cycle later); we prefer it to the fail-OPEN
    # alternative of never pruning, which would let a lost repo keep granting access for a full
    # hard-TTL window.
    if universe:
        universe_slugs = [r.slug for r in universe]
        RepositoryAccess.objects.filter(provider=provider).exclude(repo_id__in=universe_slugs).delete()
        RepositoryCatalog.objects.filter(provider=provider).exclude(slug__in=universe_slugs).delete()
    elif repos_with_rows:
        # Empty listing while rows exist is a degraded response, not a real "no repos" state:
        # skip the destructive prune and mark the run failed so it does not read as clean.
        failures += 1
        logger.warning("Repository access sync: repository universe is empty but rows exist; skipping prune")

    # Drop rows aged past the hard TTL. They already grant no access (the authorization filter
    # ignores them), so this is access-neutral; it bounds table growth and makes the empty-member
    # guard self-terminating — once a genuinely member-less repo's stale rows expire and are
    # cleared, it no longer has "prior rows" and stops being flagged as degraded.
    RepositoryAccess.objects.filter(provider=provider).stale().delete()

    if failures:
        state.status = RepositoryAccessSyncState.Status.FAILED
    else:
        state.status = RepositoryAccessSyncState.Status.OK
        state.last_success_at = timezone.now()
    state.save(update_fields=["status", "last_success_at"])


@task(dedup=True)
async def address_issue_task(
    repo_id: str,
    issue_iid: int,
    mention_comment_id: str | None = None,
    ref: str | None = None,
    thread_id: str | None = None,
    sandbox_environment_id: str | None = None,
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
        sandbox_environment_id (str | None): Per-run sandbox env id resolved at webhook time.
            When ``None``, ``set_runtime_ctx`` auto-resolves via
            :func:`sandbox_envs.services.resolve_env_for_run` (USER tier skipped) and ultimately
            falls back to the GLOBAL ``is_default=True`` env — so a non-None env may still apply.
    """
    client = RepoClient.create_instance()
    issue = client.get_issue(repo_id, issue_iid)
    async with set_runtime_ctx(
        repo_id, scope=Scope.ISSUE, ref=ref, issue=issue, sandbox_env_id=sandbox_environment_id
    ) as runtime_ctx:
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
    repo_id: str,
    merge_request_id: int,
    mention_comment_id: str,
    thread_id: str | None = None,
    sandbox_environment_id: str | None = None,
) -> AgentResult:
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        mention_comment_id (str): The mention comment id.
        thread_id (str | None): The LangGraph checkpoint key minted by the caller. When ``None``
            the addressor recomputes it from the runtime context.
        sandbox_environment_id (str | None): Per-run sandbox env id resolved at webhook time.
            When ``None``, ``set_runtime_ctx`` auto-resolves via
            :func:`sandbox_envs.services.resolve_env_for_run` (USER tier skipped) and ultimately
            falls back to the GLOBAL ``is_default=True`` env — so a non-None env may still apply.
    """
    client = RepoClient.create_instance()
    merge_request = client.get_merge_request(repo_id, merge_request_id)
    async with set_runtime_ctx(
        repo_id,
        scope=Scope.MERGE_REQUEST,
        ref=merge_request.source_branch,
        merge_request=merge_request,
        sandbox_env_id=sandbox_environment_id,
    ) as runtime_ctx:
        return await CommentsAddressorManager.address_comments(
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            runtime_ctx=runtime_ctx,
            thread_id=thread_id,
        )
