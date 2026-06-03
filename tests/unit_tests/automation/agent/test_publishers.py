import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from automation.agent.git_manager import RepoStatus
from automation.agent.publishers import GitChangePublisher, PublishOutcome
from codebase.base import GitPlatform, MergeRequest, User
from core.constants import BOT_AUTO_LABEL, BOT_NAME


def _fake_git_manager(*, dirty: bool = True, diff: str = "diff", remote_branches=(), has_unpushed: bool = True) -> Mock:
    """A stand-in for the (sandbox/local) GitManager the publisher opens via open_git_manager.

    The publisher reads everything it needs from a single ``status_snapshot``; the mutation methods
    (``commit_all``/``push_head_to``) stay separate AsyncMocks.
    """
    gm = Mock()
    gm.status_snapshot = AsyncMock(
        return_value=RepoStatus(
            dirty=dirty, diff=diff, remote_branches=list(remote_branches), has_unpushed=has_unpushed
        )
    )
    gm.commit_all = AsyncMock()
    gm.push_head_to = AsyncMock(return_value="pushed")
    gm.unique_branch_name = Mock(side_effect=lambda name, existing: name)
    return gm


def _patch_open_git_manager(monkeypatch, gm: Mock) -> None:
    @asynccontextmanager
    async def _fake_open(*, client, session_id, gitrepo):  # noqa: ARG001
        yield gm

    monkeypatch.setattr("automation.agent.publishers.open_git_manager", _fake_open)


def _make_merge_request(**overrides) -> MergeRequest:
    defaults = {
        "repo_id": "owner/repo",
        "merge_request_id": 42,
        "source_branch": "feature",
        "target_branch": "main",
        "title": "Test MR",
        "description": "Test description",
        "web_url": "https://example.com/owner/repo/-/merge_requests/42",
        "author": User(id=1, username="testuser"),
    }
    defaults.update(overrides)
    return MergeRequest(**defaults)


def _make_publisher(*, git_platform: GitPlatform = GitPlatform.GITLAB, context_file_name: str | None = "AGENTS.md"):
    ctx = Mock()
    ctx.repository.slug = "owner/repo"
    ctx.repository.html_url = "https://gitlab.com/owner/repo"
    ctx.repository.git_platform = git_platform
    ctx.config.context_file_name = context_file_name
    ctx.config.suggest_context_file = True
    ctx.config.default_branch = "main"
    ctx.git_platform = git_platform

    if git_platform == GitPlatform.GITHUB:
        ctx.repository.html_url = "https://github.com/owner/repo"

    publisher = GitChangePublisher(ctx)
    publisher.client = Mock()
    publisher.client.is_branch_protected.return_value = False
    return publisher


class TestSuggestContextFile:
    async def test_posts_comment_when_file_missing(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.return_value = None
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once_with("owner/repo", "AGENTS.md", ref="main")
        publisher.client.create_merge_request_comment.assert_called_once()
        comment_body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "AGENTS.md" in comment_body
        assert BOT_NAME in comment_body

    async def test_skips_when_file_exists(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.return_value = "# AGENTS.md content"
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_context_file_name_none(self):
        publisher = _make_publisher(context_file_name=None)
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_context_file_name_empty(self):
        publisher = _make_publisher(context_file_name="")
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_disabled_per_repo(self):
        publisher = _make_publisher()
        publisher.ctx.config.suggest_context_file = False
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_globally_disabled(self, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "suggest_context_file_enabled", False)
        publisher = _make_publisher()
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_does_not_raise_on_error(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.side_effect = Exception("API error")
        mr = _make_merge_request()

        # Should not raise
        await publisher._suggest_context_file(mr)

    async def test_custom_context_file_name(self):
        publisher = _make_publisher(context_file_name="CLAUDE.md")
        publisher.client.get_repository_file.return_value = None
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once_with("owner/repo", "CLAUDE.md", ref="main")
        comment_body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "CLAUDE.md" in comment_body


class TestCreateMergeRequestDescription:
    """The new MR description back-links to the original protected MR for traceability."""

    def _make_publisher_with_no_issue(self, *, git_platform: GitPlatform = GitPlatform.GITLAB) -> GitChangePublisher:
        publisher = _make_publisher(git_platform=git_platform)
        publisher.ctx.issue = None
        publisher.ctx.bot_username = "daiv"
        return publisher

    async def test_includes_back_link_when_fallback_provided(self):
        publisher = self._make_publisher_with_no_issue()
        original = _make_merge_request(
            source_branch="dev", merge_request_id=42, web_url="https://gitlab.com/owner/repo/-/merge_requests/42"
        )

        await publisher._create_merge_request("feature-fix", "Title", "Body", as_draft=False, fallback_from_mr=original)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "dev" in description
        assert "https://gitlab.com/owner/repo/-/merge_requests/42" in description
        assert "!42" in description

    async def test_omits_back_link_when_no_fallback(self):
        publisher = self._make_publisher_with_no_issue()

        await publisher._create_merge_request("feature", "Title", "Body", as_draft=False)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "is protected on the remote" not in description

    async def test_back_link_uses_github_terminology(self):
        publisher = self._make_publisher_with_no_issue(git_platform=GitPlatform.GITHUB)
        original = _make_merge_request(
            source_branch="main", merge_request_id=10, web_url="https://github.com/owner/repo/pull/10"
        )

        await publisher._create_merge_request("feature-fix", "Title", "Body", as_draft=False, fallback_from_mr=original)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "#10" in description
        assert "!10" not in description


class TestBuildIssueCreationUrl:
    def test_gitlab_url_format(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)

        url = publisher._build_issue_creation_url("AGENTS.md")

        parsed = urlparse(url)
        assert parsed.path == "/owner/repo/-/issues/new"
        params = parse_qs(parsed.query)
        assert "issue[title]" in params
        assert "AGENTS.md" in params["issue[title]"][0]
        assert "issue[description]" in params
        assert "/label ~" + BOT_AUTO_LABEL in params["issue[description]"][0]

    def test_github_url_format(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITHUB)

        url = publisher._build_issue_creation_url("AGENTS.md")

        parsed = urlparse(url)
        assert parsed.path == "/owner/repo/issues/new"
        params = parse_qs(parsed.query)
        assert "title" in params
        assert "AGENTS.md" in params["title"][0]
        assert "body" in params
        assert "labels" in params
        assert params["labels"][0] == BOT_AUTO_LABEL


class TestPublishSuggestsContextFile:
    @pytest.fixture
    def publisher(self):
        pub = _make_publisher()
        pub.client.get_repository_file.return_value = None
        return pub

    async def test_calls_suggest_on_new_mr(self, publisher, monkeypatch):
        mr = _make_merge_request()
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            result = await publisher.publish(merge_request=None)

            gm.commit_all.assert_awaited_once()
            gm.push_head_to.assert_awaited_once_with("feature")
            mock_suggest.assert_called_once_with(mr)
            assert result == PublishOutcome(merge_request=mr, published=True)

    async def test_falls_back_to_new_mr_when_source_branch_protected(self, publisher, monkeypatch):
        existing_mr = _make_merge_request(source_branch="dev", merge_request_id=42)
        new_mr = _make_merge_request(source_branch="feature-fix", merge_request_id=43)
        publisher.client.is_branch_protected.return_value = True
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature-fix", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ) as mock_diff_to_metadata,
            patch.object(publisher, "_create_merge_request", return_value=new_mr) as mock_create_mr,
            patch.object(publisher, "_suggest_context_file"),
        ):
            result = await publisher.publish(merge_request=existing_mr)

            publisher.client.is_branch_protected.assert_called_once_with("owner/repo", "dev")
            # Pre-check must run before _diff_to_metadata so the fallback path receives a
            # populated pr_metadata_diff (the new MR needs title/branch/description).
            assert mock_diff_to_metadata.call_args.kwargs["pr_metadata_diff"] is not None
            # Fresh unique branch generated + pushed for the fallback MR.
            gm.unique_branch_name.assert_called_once_with("feature-fix", [])
            gm.push_head_to.assert_awaited_once_with("feature-fix")
            # The new MR is created with a back-link to the original protected MR.
            mock_create_mr.assert_called_once()
            assert mock_create_mr.call_args.kwargs["fallback_from_mr"] is existing_mr
            # Fallback source is exposed on the publisher so the manager can bundle a
            # footer onto the agent's reply instead of posting a separate comment.
            assert publisher.protected_branch_fallback_source == "dev"
            # No fallback comment is posted from the publisher itself.
            publisher.client.create_merge_request_comment.assert_not_called()
            assert result == PublishOutcome(merge_request=new_mr, published=True)

    async def test_protected_branch_fallback_resets_between_publish_calls(self, publisher, monkeypatch):
        # First call hits the protected branch and sets the attribute; a follow-up
        # publish on a clean branch must not leave the prior signal behind.
        existing_mr = _make_merge_request(source_branch="dev", merge_request_id=42)
        new_mr = _make_merge_request(source_branch="feature-fix", merge_request_id=43)
        publisher.client.is_branch_protected.return_value = True
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature-fix", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_create_merge_request", return_value=new_mr),
            patch.object(publisher, "_suggest_context_file"),
        ):
            await publisher.publish(merge_request=existing_mr)
            assert publisher.protected_branch_fallback_source == "dev"

            # Second call: clean tree, no diff versus base → publish short-circuits, but
            # the signal from the prior call must still clear.
            gm.status_snapshot.return_value = RepoStatus(dirty=False, diff="", remote_branches=[], has_unpushed=False)
            await publisher.publish(merge_request=None)
            assert publisher.protected_branch_fallback_source is None

    async def test_publish_propagates_push_failure_without_creating_mr(self, publisher, monkeypatch):
        """If the daiv-direct push fails, publish() must propagate (fail loud) and NOT open an MR
        against a branch that never landed on the remote."""
        from automation.agent.git_manager import GitPushNetworkError

        gm = _fake_git_manager()
        gm.push_head_to = AsyncMock(side_effect=GitPushNetworkError("no network"))
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_create_merge_request") as mock_create,
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            with pytest.raises(GitPushNetworkError):
                await publisher.publish(merge_request=None)

            mock_create.assert_not_called()
            mock_suggest.assert_not_called()

    async def test_does_not_suggest_on_existing_mr(self, publisher, monkeypatch):
        mr = _make_merge_request()
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            await publisher.publish(merge_request=mr)

            gm.push_head_to.assert_awaited_once_with("feature")
            mock_suggest.assert_not_called()


class TestPublishDecision:
    """The publish decision (formerly GitMiddleware._is_unpublished) now lives in publish()."""

    async def test_returns_nothing_when_clean_and_no_diff(self, monkeypatch):
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(session_id="s", merge_request=None)

        assert outcome == PublishOutcome(merge_request=None, published=False)
        meta.assert_not_called()
        gm.push_head_to.assert_not_called()

    async def test_confirms_existing_mr_without_republishing_when_pushed(self, monkeypatch):
        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", merge_request_id=42)
        gm = _fake_git_manager(dirty=False, diff="diff", remote_branches=["feat/x"], has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(session_id="s", merge_request=mr)

        assert outcome == PublishOutcome(merge_request=mr, published=False)
        meta.assert_not_called()
        gm.push_head_to.assert_not_called()


def test_create_merge_request_and_suggest_are_async():
    assert inspect.iscoroutinefunction(GitChangePublisher._create_merge_request)
    assert inspect.iscoroutinefunction(GitChangePublisher._suggest_context_file)
