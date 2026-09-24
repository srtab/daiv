from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from github.GithubException import GithubException
from gitlab.exceptions import GitlabError

import codebase.tasks as codebase_tasks
from codebase.base import MergeRequest, MergeRequestCommit, MergeRequestDiffStats, User
from codebase.exceptions import CloneRefNotFoundError


def _mr(*, merged: bool) -> MergeRequest:
    return MergeRequest(
        repo_id="group/repo",
        merge_request_id=7,
        source_branch="chore/x",
        target_branch="dev",
        title="t",
        description="d",
        web_url="https://git/group/repo/-/merge_requests/7",
        author=User(id=1, username="u", name="U"),
        merged=merged,
    )


async def test_address_mr_comments_skips_when_merged():
    from codebase.tasks import address_mr_comments_task

    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=True)

    with (
        patch("codebase.tasks.RepoClient.create_instance", return_value=client),
        patch("codebase.managers.review_addressor.CommentsAddressorManager.address_comments") as address,
    ):
        result = await address_mr_comments_task.func(repo_id="group/repo", merge_request_id=7, mention_comment_id="d1")

    address.assert_not_called()
    client.create_merge_request_comment.assert_called_once()
    assert "already been merged" in result["response"]
    assert result["code_changes"] is False


async def test_address_mr_comments_skips_when_branch_gone():
    from codebase.tasks import address_mr_comments_task

    client = MagicMock()
    client.get_merge_request.return_value = _mr(merged=False)

    with (
        patch("codebase.tasks.RepoClient.create_instance", return_value=client),
        patch(
            "codebase.managers.review_addressor.CommentsAddressorManager.address_comments",
            AsyncMock(side_effect=CloneRefNotFoundError("chore/x", "group/repo")),
        ),
    ):
        result = await address_mr_comments_task.func(repo_id="group/repo", merge_request_id=7, mention_comment_id="d1")

    client.create_merge_request_comment.assert_called_once()
    assert "no longer exists" in result["response"]
    assert result["code_changes"] is False


async def test_address_mr_comments_hands_the_merge_request_to_the_addressor():
    from codebase.tasks import address_mr_comments_task

    client = MagicMock()
    merge_request = _mr(merged=False)
    client.get_merge_request.return_value = merge_request
    address = AsyncMock(return_value={"response": "done"})

    with (
        patch("codebase.tasks.RepoClient.create_instance", return_value=client),
        patch("codebase.managers.review_addressor.CommentsAddressorManager.address_comments", address),
    ):
        result = await address_mr_comments_task.func(
            repo_id="group/repo",
            merge_request_id=7,
            mention_comment_id="d1",
            thread_id="t-7",
            sandbox_environment_id="e",
        )

    assert result == {"response": "done"}
    assert address.await_args.kwargs == {
        "repo_id": "group/repo",
        "merge_request": merge_request,
        "mention_comment_id": "d1",
        "thread_id": "t-7",
        "sandbox_env_id": "e",
    }


class TestAddressIssueTaskRef:
    """An issue webhook carries no ref of its own — an issue is not a branch. ``Session.ref`` is what carries an
    issue session's working branch from one turn to the next: without it a follow-up turn re-clones the default
    branch while the checkpoint still names the branch the previous turn published to, and the publish then pushes
    a sibling commit onto it. The re-pin after a vanished branch is the executor's (``TestRefFallback`` in
    ``tests/unit_tests/sessions/executor/test_run.py``)."""

    @staticmethod
    async def _addressed(*, session_ref: str, ref: str | None = None) -> AsyncMock:
        from codebase.tasks import address_issue_task

        client = MagicMock()
        client.get_issue.return_value = MagicMock()
        addressed = AsyncMock(return_value={"response": "", "code_changes": False})
        with (
            patch("codebase.tasks.RepoClient.create_instance", return_value=client),
            patch("sessions.services.aget_session_ref", AsyncMock(return_value=session_ref)),
            patch("codebase.managers.issue_addressor.IssueAddressorManager.address_issue", addressed),
        ):
            await address_issue_task.func(
                repo_id="group/repo",
                issue_iid=10,
                mention_comment_id="d1",
                thread_id="t-1",
                ref=ref,
                sandbox_environment_id="e",
            )

        addressed.issue = client.get_issue.return_value
        return addressed

    async def test_a_follow_up_turn_clones_the_branch_the_session_works_on(self):
        addressed = await self._addressed(session_ref="fix/10-update-dependencies")
        assert addressed.await_args.kwargs["ref"] == "fix/10-update-dependencies"

    async def test_a_first_turn_still_clones_the_default_branch(self):
        """Nothing published yet, so there is no working branch to resume — ``None`` lets the clone pick the
        repository default."""
        addressed = await self._addressed(session_ref="")
        assert addressed.await_args.kwargs["ref"] is None

    async def test_an_explicit_ref_wins_over_the_session(self):
        addressed = await self._addressed(session_ref="fix/10", ref="release/1.2")
        assert addressed.await_args.kwargs["ref"] == "release/1.2"

    async def test_the_task_hands_the_issue_to_the_addressor(self):
        addressed = await self._addressed(session_ref="")
        assert addressed.await_args.kwargs == {
            "repo_id": "group/repo",
            "issue": addressed.issue,
            "mention_comment_id": "d1",
            "ref": None,
            "thread_id": "t-1",
            "sandbox_env_id": "e",
        }


async def test_setup_webhooks_cron_task_calls_command():
    if not hasattr(codebase_tasks, "setup_webhooks_cron_task"):
        pytest.skip("setup_webhooks_cron_task is only defined for the GitLab client")

    with patch("codebase.tasks.call_command") as mock_call_command, patch("codebase.tasks.settings.DEBUG", True):
        await codebase_tasks.setup_webhooks_cron_task.aenqueue()

    mock_call_command.assert_called_once_with("setup_webhooks", disable_ssl_verification=True)


_TASK_DEFAULTS = {
    "repo_id": "owner/repo",
    "merge_request_iid": 42,
    "title": "feat: add feature",
    "source_branch": "feat/something",
    "target_branch": "main",
    "merged_at": "2026-04-01T10:00:00+00:00",
    "platform": "gitlab",
}

# Call the underlying coroutine directly, bypassing the task framework
_run_task = codebase_tasks.record_merge_metrics_task.func

_BOT_EMAIL = "daiv@users.noreply.gitlab.com"


@pytest.mark.django_db(transaction=True)
class TestRecordMergeMetricsTask:
    """Tests for record_merge_metrics_task."""

    @pytest.fixture(autouse=True)
    def _setup(self, mock_repo_client):
        mock_repo_client.get_merge_request_diff_stats.return_value = MergeRequestDiffStats(
            lines_added=100, lines_removed=50, files_changed=5
        )
        mock_repo_client.get_merge_request_commits.return_value = []
        mock_repo_client.get_bot_commit_email.return_value = _BOT_EMAIL
        self.mock_client = mock_repo_client

    async def test_records_metric_with_diff_stats(self):
        """Test that task records merge metric with diff stats from client."""
        result = await _run_task(**_TASK_DEFAULTS)

        assert result == {"recorded": True}
        self.mock_client.get_merge_request_diff_stats.assert_called_once_with("owner/repo", 42)

    async def test_stores_diff_stats_from_client(self):
        """Test that lines_added, lines_removed, files_changed come from client."""
        from codebase.models import MergeMetric

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.lines_added == 100
        assert metric.lines_removed == 50
        assert metric.files_changed == 5

    async def test_all_daiv_commits(self):
        """Test that all commits authored by bot email are attributed to DAIV."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_commits.return_value = [
            MergeRequestCommit(sha="abc", author_email=_BOT_EMAIL, lines_added=60, lines_removed=30),
            MergeRequestCommit(sha="def", author_email=_BOT_EMAIL, lines_added=40, lines_removed=20),
        ]

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.daiv_lines_added == 100
        assert metric.daiv_lines_removed == 50
        assert metric.human_lines_added == 0
        assert metric.human_lines_removed == 0
        assert metric.total_commits == 2
        assert metric.daiv_commits == 2

    async def test_all_human_commits(self):
        """Test that commits not authored by bot email are attributed to humans."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_commits.return_value = [
            MergeRequestCommit(sha="abc", author_email="human@example.com", lines_added=100, lines_removed=50)
        ]

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.daiv_lines_added == 0
        assert metric.daiv_lines_removed == 0
        assert metric.human_lines_added == 100
        assert metric.human_lines_removed == 50
        assert metric.total_commits == 1
        assert metric.daiv_commits == 0

    async def test_mixed_commits(self):
        """Test that mixed DAIV and human commits are split correctly."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_commits.return_value = [
            MergeRequestCommit(sha="abc", author_email=_BOT_EMAIL, lines_added=70, lines_removed=10),
            MergeRequestCommit(sha="def", author_email="human@example.com", lines_added=30, lines_removed=40),
        ]

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.daiv_lines_added == 70
        assert metric.daiv_lines_removed == 10
        assert metric.human_lines_added == 30
        assert metric.human_lines_removed == 40
        assert metric.total_commits == 2
        assert metric.daiv_commits == 1

    async def test_bot_email_case_insensitive(self):
        """Test that bot email matching is case-insensitive."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_commits.return_value = [
            MergeRequestCommit(sha="abc", author_email=_BOT_EMAIL.upper(), lines_added=50, lines_removed=25)
        ]

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.daiv_commits == 1

    async def test_commit_fetch_failure_fallback(self):
        """Test that commit fetch failure records zero DAIV attribution."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_commits.side_effect = GitlabError("API error")

        result = await _run_task(**_TASK_DEFAULTS)

        assert result == {"recorded": True}
        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.lines_added == 100  # diff stats still work
        assert metric.daiv_lines_added == 0
        assert metric.total_commits == 0

    async def test_diff_stats_failure_uses_commit_sums(self):
        """Test that when diff stats fail but commits succeed, commit sums are used as totals."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_diff_stats.side_effect = GitlabError("API error")
        self.mock_client.get_merge_request_commits.return_value = [
            MergeRequestCommit(sha="abc", author_email="human@example.com", lines_added=80, lines_removed=20)
        ]

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.lines_added == 80
        assert metric.lines_removed == 20

    async def test_falls_back_to_zero_on_diff_stats_error(self):
        """Test that diff stats error records zero stats."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_diff_stats.side_effect = GithubException(500, "Server error", None)

        result = await _run_task(**{**_TASK_DEFAULTS, "platform": "github"})

        assert result == {"recorded": True}
        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42, platform="github")
        assert metric.lines_added == 0

    async def test_both_api_calls_fail(self):
        """Test that when both diff stats and commits fail, metric is recorded with all zeros."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_diff_stats.side_effect = GitlabError("API error")
        self.mock_client.get_merge_request_commits.side_effect = GitlabError("API error")

        result = await _run_task(**_TASK_DEFAULTS)

        assert result == {"recorded": True}
        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.lines_added == 0
        assert metric.lines_removed == 0
        assert metric.files_changed == 0
        assert metric.daiv_lines_added == 0
        assert metric.total_commits == 0
        assert metric.daiv_commits == 0

    async def test_bot_email_failure_still_records_commits(self):
        """Test that bot email failure still records commits (all attributed to human)."""
        from codebase.models import MergeMetric

        self.mock_client.get_bot_commit_email.side_effect = GitlabError("Auth error")
        self.mock_client.get_merge_request_commits.return_value = [
            MergeRequestCommit(sha="abc", author_email=_BOT_EMAIL, lines_added=50, lines_removed=25)
        ]

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.total_commits == 1
        assert metric.daiv_commits == 0  # can't identify bot, so all human
        assert metric.human_lines_added == 50

    async def test_zero_line_mr_not_overwritten_by_commit_sums(self):
        """Test that a legitimate zero-line MR keeps zeros when diff stats succeed."""
        from codebase.models import MergeMetric

        self.mock_client.get_merge_request_diff_stats.return_value = MergeRequestDiffStats(
            lines_added=0, lines_removed=0, files_changed=3
        )
        self.mock_client.get_merge_request_commits.return_value = [
            MergeRequestCommit(sha="abc", author_email="human@example.com", lines_added=5, lines_removed=2)
        ]

        await _run_task(**_TASK_DEFAULTS)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.lines_added == 0  # diff stats succeeded with 0, not overwritten
        assert metric.lines_removed == 0
        assert metric.files_changed == 3

    async def test_parses_valid_merged_at_timestamp(self):
        """Test that a valid ISO timestamp is parsed correctly."""
        from codebase.models import MergeMetric

        await _run_task(**{**_TASK_DEFAULTS, "merged_at": "2026-03-15T14:30:00+00:00"})

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.merged_at.year == 2026
        assert metric.merged_at.month == 3
        assert metric.merged_at.day == 15

    async def test_falls_back_to_now_on_invalid_merged_at(self):
        """Test that an invalid merged_at string falls back to current time."""
        from codebase.models import MergeMetric

        before = datetime.now(tz=UTC)
        await _run_task(**{**_TASK_DEFAULTS, "merged_at": "not-a-date"})
        after = datetime.now(tz=UTC)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert before - timedelta(seconds=1) <= metric.merged_at <= after + timedelta(seconds=1)

    async def test_falls_back_to_now_on_empty_merged_at(self):
        """Test that an empty merged_at string falls back to current time."""
        from codebase.models import MergeMetric

        before = datetime.now(tz=UTC)
        await _run_task(**{**_TASK_DEFAULTS, "merged_at": ""})
        after = datetime.now(tz=UTC)

        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert before - timedelta(seconds=1) <= metric.merged_at <= after + timedelta(seconds=1)

    async def test_upserts_existing_metric(self):
        """Test that a duplicate webhook updates the existing record instead of creating a second one."""
        from codebase.models import MergeMetric

        await _run_task(**_TASK_DEFAULTS)
        # Change the diff stats and re-run
        self.mock_client.get_merge_request_diff_stats.return_value = MergeRequestDiffStats(
            lines_added=200, lines_removed=100, files_changed=10
        )
        await _run_task(**_TASK_DEFAULTS)

        assert await MergeMetric.objects.acount() == 1
        metric = await MergeMetric.objects.aget(repo_id="owner/repo", merge_request_iid=42)
        assert metric.lines_added == 200
