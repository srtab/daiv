from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from automation.agent.publishers import GitChangePublisher
from codebase.base import GitPlatform, MergeRequest, User
from core.constants import BOT_AUTO_LABEL, BOT_NAME


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
    def test_posts_comment_when_file_missing(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.return_value = None
        mr = _make_merge_request()

        publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once_with("owner/repo", "AGENTS.md", ref="main")
        publisher.client.create_merge_request_comment.assert_called_once()
        comment_body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "AGENTS.md" in comment_body
        assert BOT_NAME in comment_body

    def test_skips_when_file_exists(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.return_value = "# AGENTS.md content"
        mr = _make_merge_request()

        publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once()
        publisher.client.create_merge_request_comment.assert_not_called()

    def test_skips_when_context_file_name_none(self):
        publisher = _make_publisher(context_file_name=None)
        mr = _make_merge_request()

        publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    def test_skips_when_context_file_name_empty(self):
        publisher = _make_publisher(context_file_name="")
        mr = _make_merge_request()

        publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    def test_skips_when_disabled_per_repo(self):
        publisher = _make_publisher()
        publisher.ctx.config.suggest_context_file = False
        mr = _make_merge_request()

        publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    def test_skips_when_globally_disabled(self, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "suggest_context_file_enabled", False)
        publisher = _make_publisher()
        mr = _make_merge_request()

        publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    def test_does_not_raise_on_error(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.side_effect = Exception("API error")
        mr = _make_merge_request()

        # Should not raise
        publisher._suggest_context_file(mr)

    def test_custom_context_file_name(self):
        publisher = _make_publisher(context_file_name="CLAUDE.md")
        publisher.client.get_repository_file.return_value = None
        mr = _make_merge_request()

        publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once_with("owner/repo", "CLAUDE.md", ref="main")
        comment_body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "CLAUDE.md" in comment_body


class TestNotifyProtectedBranchFallback:
    def test_posts_comment_linking_to_new_mr(self):
        publisher = _make_publisher()
        original = _make_merge_request(source_branch="dev", merge_request_id=42)
        new_mr = _make_merge_request(
            source_branch="feature-fix",
            merge_request_id=43,
            web_url="https://gitlab.com/owner/repo/-/merge_requests/43",
        )

        publisher._notify_protected_branch_fallback(original, new_mr)

        publisher.client.create_merge_request_comment.assert_called_once()
        repo_id, mr_id, body = publisher.client.create_merge_request_comment.call_args[0]
        assert repo_id == "owner/repo"
        assert mr_id == 42
        assert "dev" in body
        assert "https://gitlab.com/owner/repo/-/merge_requests/43" in body
        assert "!43" in body

    def test_does_not_raise_when_comment_post_fails(self):
        publisher = _make_publisher()
        publisher.client.create_merge_request_comment.side_effect = Exception("boom")
        original = _make_merge_request(merge_request_id=42)
        new_mr = _make_merge_request(merge_request_id=43)

        # Best-effort: a failed courtesy comment must not tear down a successful publish.
        publisher._notify_protected_branch_fallback(original, new_mr)

    def test_renders_github_terminology_for_github_platform(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITHUB)
        original = _make_merge_request(source_branch="main", merge_request_id=10)
        new_mr = _make_merge_request(
            source_branch="feature-fix", merge_request_id=11, web_url="https://github.com/owner/repo/pull/11"
        )

        publisher._notify_protected_branch_fallback(original, new_mr)

        body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "pull request" in body
        assert "#11" in body


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

    async def test_calls_suggest_on_new_mr(self, publisher):
        mr = _make_merge_request()

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch("automation.agent.publishers.GitManager") as mock_git_mgr_cls,
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            mock_git_mgr = mock_git_mgr_cls.return_value
            mock_git_mgr.is_dirty.return_value = True
            mock_git_mgr.get_diff.return_value = "diff"
            mock_git_mgr.commit_and_push_changes.return_value = "feature"

            result = await publisher.publish(merge_request=None)

            mock_suggest.assert_called_once_with(mr)
            assert result == mr

    async def test_falls_back_to_new_mr_when_source_branch_protected(self, publisher):
        existing_mr = _make_merge_request(source_branch="dev", merge_request_id=42)
        new_mr = _make_merge_request(source_branch="feature-fix", merge_request_id=43)
        publisher.client.is_branch_protected.return_value = True

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature-fix", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ) as mock_diff_to_metadata,
            patch("automation.agent.publishers.GitManager") as mock_git_mgr_cls,
            patch.object(publisher, "_create_merge_request", return_value=new_mr) as mock_create_mr,
            patch.object(publisher, "_suggest_context_file"),
            patch.object(publisher, "_notify_protected_branch_fallback") as mock_notify,
        ):
            mock_git_mgr = mock_git_mgr_cls.return_value
            mock_git_mgr.is_dirty.return_value = True
            mock_git_mgr.get_diff.return_value = "diff"
            mock_git_mgr.commit_and_push_changes.return_value = "feature-fix"

            result = await publisher.publish(merge_request=existing_mr)

            publisher.client.is_branch_protected.assert_called_once_with("owner/repo", "dev")
            # Pre-check must run before _diff_to_metadata so the fallback path receives a
            # populated pr_metadata_diff (the new MR needs title/branch/description).
            assert mock_diff_to_metadata.call_args.kwargs["pr_metadata_diff"] is not None
            mock_git_mgr.commit_and_push_changes.assert_called_once_with(
                "commit msg", branch_name="feature-fix", use_branch_if_exists=False, skip_ci=False
            )
            mock_create_mr.assert_called_once()
            mock_notify.assert_called_once_with(existing_mr, new_mr)
            assert result == new_mr

    async def test_does_not_suggest_on_existing_mr(self, publisher):
        mr = _make_merge_request()

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch("automation.agent.publishers.GitManager") as mock_git_mgr_cls,
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            mock_git_mgr = mock_git_mgr_cls.return_value
            mock_git_mgr.is_dirty.return_value = True
            mock_git_mgr.get_diff.return_value = "diff"
            mock_git_mgr.commit_and_push_changes.return_value = "feature"

            await publisher.publish(merge_request=mr)

            mock_suggest.assert_not_called()
