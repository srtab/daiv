from unittest.mock import Mock, patch

import pytest
from github import UnknownObjectException
from github.GithubException import GithubException
from github.IssueComment import IssueComment
from github.PullRequestComment import PullRequestComment

from codebase.base import GitPlatform, MergeRequestCommit, Repository, User
from codebase.clients.base import Emoji
from codebase.clients.github.client import GitHubClient


class TestGitHubClient:
    """Tests for GitHubClient."""

    @pytest.fixture
    def github_client(self):
        """Create a GitHubClient instance with mocked dependencies."""
        integration = Mock()
        mock_installation = Mock()
        mock_github = Mock()
        mock_github.requester.auth.token = "test-token-123"  # noqa: S105

        mock_installation.get_github_for_installation.return_value = mock_github
        integration.get_app_installation.return_value = mock_installation

        client = GitHubClient(integration=integration, installation_id=67890)
        yield client

    @patch("codebase.clients.github.client.async_download_url")
    async def test_get_project_uploaded_file_success(self, mock_download, github_client):
        """Test successful download of GitHub user-attachments file."""
        mock_download.return_value = b"image content"

        url = "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e.png"
        result = await github_client.get_project_uploaded_file("owner/repo", url)

        assert result == b"image content"
        mock_download.assert_called_once_with(url, headers={"Authorization": "Bearer test-token-123"})

    @patch("codebase.clients.github.client.async_download_url")
    async def test_get_project_uploaded_file_failure(self, mock_download, github_client):
        """Test failed download returns None."""
        mock_download.return_value = None

        url = "https://github.com/user-attachments/assets/invalid.png"
        result = await github_client.get_project_uploaded_file("owner/repo", url)

        assert result is None
        mock_download.assert_called_once_with(url, headers={"Authorization": "Bearer test-token-123"})

    def test_create_issue_emoji_converts_note_id_to_int(self, github_client):
        """Test that create_issue_emoji converts string note_id to int."""
        mock_repo = Mock()
        mock_issue = Mock()
        mock_comment = Mock()

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_comment.return_value = mock_comment

        # Pass note_id as a string
        github_client.create_issue_emoji("owner/repo", 123, Emoji.THUMBSUP, 3645723306)

        # Verify that get_comment was called with an integer
        mock_issue.get_comment.assert_called_once_with(3645723306)
        mock_comment.create_reaction.assert_called_once_with("+1")

    @pytest.mark.parametrize(
        ("reactions", "emoji", "expected"),
        [
            pytest.param([("eyes", 456), ("eyes", 123)], Emoji.EYES, True, id="reaction-exists-for-current-user"),
            pytest.param([("eyes", 456)], Emoji.EYES, False, id="reaction-from-different-user"),
            pytest.param([("+1", 123)], Emoji.EYES, False, id="different-emoji"),
            pytest.param([], Emoji.EYES, False, id="no-reactions"),
        ],
    )
    def test_has_issue_reaction(self, github_client, monkeypatch, reactions, emoji, expected):
        """Test issue reaction matching for user and emoji combinations."""
        mock_repo = Mock()
        mock_issue = Mock()
        mock_reactions = []
        for content, user_id in reactions:
            reaction = Mock()
            reaction.content = content
            reaction.user = Mock(id=user_id)
            mock_reactions.append(reaction)

        monkeypatch.setattr(type(github_client), "current_user", User(id=123, username="daiv", name="DAIV"))
        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_reactions.return_value = mock_reactions

        result = github_client.has_issue_reaction("owner/repo", 123, emoji)
        assert result is expected

    def test_create_merge_request_note_emoji_review_comment(self, github_client):
        """Test that create_merge_request_note_emoji converts string note_id to int for review comments."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_comment = Mock()

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        mock_pr.get_review_comment.return_value = mock_comment

        # Pass note_id as a string
        github_client.create_merge_request_note_emoji("owner/repo", 712, Emoji.THUMBSUP, 3645723306)

        # Verify that get_review_comment was called with an integer
        mock_pr.get_review_comment.assert_called_once_with(3645723306)
        mock_comment.create_reaction.assert_called_once_with("+1")

    def test_create_merge_request_note_emoji_issue_comment_fallback(self, github_client):
        """Test that create_merge_request_note_emoji falls back to issue comment when review comment not found."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_comment = Mock()

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        # Simulate review comment not found
        mock_pr.get_review_comment.side_effect = UnknownObjectException(404, {}, {})
        mock_pr.get_issue_comment.return_value = mock_comment

        # Pass note_id as a string
        github_client.create_merge_request_note_emoji("owner/repo", 712, Emoji.THUMBSUP, 3645723306)

        # Verify that both methods were called with an integer
        mock_pr.get_review_comment.assert_called_once_with(3645723306)
        mock_pr.get_issue_comment.assert_called_once_with(3645723306)
        mock_comment.create_reaction.assert_called_once_with("+1")

    def test_get_merge_request_comment_converts_comment_id_to_int_issue_comment(self, github_client):
        """Test that get_merge_request_comment converts string comment_id to int for issue comments."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_user = Mock()
        mock_user.id = 1
        mock_user.login = "testuser"
        mock_user.name = "Test User"

        mock_comment = Mock(spec=IssueComment)
        mock_comment.id = 3645723306
        mock_comment.body = "Test comment"
        mock_comment.user = mock_user

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        mock_pr.get_issue_comment.return_value = mock_comment

        # Pass comment_id as a string
        result = github_client.get_merge_request_comment("owner/repo", 712, "3645723306")

        # Verify that get_issue_comment was called with an integer
        mock_pr.get_issue_comment.assert_called_once_with(3645723306)
        assert result.id == "3645723306"
        assert len(result.notes) == 1

    def test_get_merge_request_comment_converts_comment_id_to_int_review_comment(self, github_client):
        """Test that get_merge_request_comment converts string comment_id to int for review comments."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_user = Mock()
        mock_user.id = 1
        mock_user.login = "testuser"
        mock_user.name = "Test User"

        mock_comment = Mock(spec=PullRequestComment)
        mock_comment.id = 3645723306
        mock_comment.body = "Test review comment"
        mock_comment.user = mock_user
        mock_comment.path = "test.py"
        mock_comment.commit_id = "abc123"
        mock_comment.line = 10
        mock_comment.start_line = None
        mock_comment.side = "RIGHT"
        mock_comment.start_side = None
        mock_comment.subject_type = "line"

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        # Simulate issue comment not found
        mock_pr.get_issue_comment.side_effect = UnknownObjectException(404, {}, {})
        mock_pr.get_review_comment.return_value = mock_comment

        # Pass comment_id as a string
        result = github_client.get_merge_request_comment("owner/repo", 712, "3645723306")

        # Verify that both methods were called with an integer
        mock_pr.get_issue_comment.assert_called_once_with(3645723306)
        mock_pr.get_review_comment.assert_called_once_with(3645723306)
        assert result.id == "3645723306"
        assert len(result.notes) == 1

    @patch("codebase.clients.github.client.Repo.clone_from")
    def test_load_repo_configures_git_identity_with_app_bot(self, mock_clone_from, github_client, monkeypatch):
        """Test load_repo configures local git identity to the app bot user."""
        mock_repo = Mock()
        mock_writer = Mock()
        mock_repo.config_writer.return_value.__enter__ = Mock(return_value=mock_writer)
        mock_repo.config_writer.return_value.__exit__ = Mock(return_value=None)
        mock_clone_from.return_value = mock_repo

        github_client.client_installation.id = 67890
        github_client.client_installation.app_slug = "daiv-agent-test"
        github_client._integration.get_access_token.return_value = Mock(token="token")  # noqa: S106
        monkeypatch.setattr(
            type(github_client), "current_user", User(id=123456, username="daiv-agent-test", name="DAIV Agent Test")
        )

        repository = Repository(
            pk=1,
            slug="owner/repo",
            name="repo",
            clone_url="https://github.com/owner/repo.git",
            html_url="https://github.com/owner/repo",
            default_branch="main",
            git_platform=GitPlatform.GITHUB,
        )

        with github_client.load_repo(repository, "main") as loaded_repo:
            assert loaded_repo == mock_repo

        clone_url, clone_dir = mock_clone_from.call_args.args[:2]
        branch = mock_clone_from.call_args.kwargs["branch"]
        assert clone_url == "https://oauth2:token@github.com/owner/repo.git"
        assert clone_dir.name == "repo"
        assert branch == "main"
        mock_writer.set_value.assert_any_call("user", "name", "daiv-agent-test[bot]")
        mock_writer.set_value.assert_any_call("user", "email", "123456+daiv-agent-test[bot]@users.noreply.github.com")

    def test_get_merge_request_commits_returns_commit_list(self, github_client):
        """Test that commits are returned with author email and stats."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_commit = Mock()
        mock_commit.sha = "abc123"
        mock_commit.commit.author.email = "dev@example.com"
        mock_commit.stats.additions = 10
        mock_commit.stats.deletions = 5
        mock_pr.get_commits.return_value = [mock_commit]
        mock_repo.get_pull.return_value = mock_pr
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.get_merge_request_commits("owner/repo", 1)

        assert result == [
            MergeRequestCommit(sha="abc123", author_email="dev@example.com", lines_added=10, lines_removed=5)
        ]

    def test_get_merge_request_commits_handles_none_author(self, github_client):
        """Test that None commit.commit.author defaults to empty email."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_commit = Mock()
        mock_commit.sha = "abc123"
        mock_commit.commit = Mock(author=None)
        mock_commit.stats.additions = 1
        mock_commit.stats.deletions = 0
        mock_pr.get_commits.return_value = [mock_commit]
        mock_repo.get_pull.return_value = mock_pr
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.get_merge_request_commits("owner/repo", 1)

        assert result[0].author_email == ""

    def test_get_merge_request_commits_handles_none_stats(self, github_client):
        """Test that None commit.stats defaults to zero line counts."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_commit = Mock()
        mock_commit.sha = "abc123"
        mock_commit.commit.author.email = "dev@example.com"
        mock_commit.stats = None
        mock_pr.get_commits.return_value = [mock_commit]
        mock_repo.get_pull.return_value = mock_pr
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.get_merge_request_commits("owner/repo", 1)

        assert result[0].lines_added == 0
        assert result[0].lines_removed == 0

    def test_get_merge_request_commits_per_commit_error_continues(self, github_client):
        """Test that a per-commit stats failure skips stats but still includes the commit."""
        mock_repo = Mock()
        mock_pr = Mock()
        commit1 = Mock()
        commit1.sha = "abc"
        commit1.commit.author.email = "dev@example.com"
        type(commit1).stats = property(lambda self: (_ for _ in ()).throw(GithubException(500, "error", None)))
        commit2 = Mock()
        commit2.sha = "def"
        commit2.commit.author.email = "dev@example.com"
        commit2.stats.additions = 5
        commit2.stats.deletions = 3
        mock_pr.get_commits.return_value = [commit1, commit2]
        mock_repo.get_pull.return_value = mock_pr
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.get_merge_request_commits("owner/repo", 1)

        assert len(result) == 2
        assert result[0].lines_added == 0  # failed commit gets zero stats
        assert result[1].lines_added == 5

    def test_get_bot_commit_email(self, github_client, monkeypatch):
        """Test that bot commit email is formatted correctly."""
        github_client.client_installation.app_slug = "daiv-bot"
        monkeypatch.setattr(type(github_client), "current_user", User(id=12345, username="daiv-bot[bot]", name="DAIV"))

        result = github_client.get_bot_commit_email()

        assert result == "12345+daiv-bot[bot]@users.noreply.github.com"

    def test_list_branches_returns_branch_names(self, github_client):
        """`list_branches` returns branch names from PyGithub's iterator."""
        mock_repo = Mock()
        branches = []
        for name in ("main", "feat/one", "fix/two"):
            branch = Mock()
            branch.name = name
            branches.append(branch)
        mock_repo.get_branches.return_value = iter(branches)
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.list_branches("owner/repo")

        assert result == ["main", "feat/one", "fix/two"]
        github_client.client.get_repo.assert_called_once_with("owner/repo", lazy=True)

    def test_list_branches_filters_client_side_case_insensitively(self, github_client):
        """Client-side substring filter is case-insensitive and preserves order."""
        mock_repo = Mock()
        branches = []
        for name in ("main", "Feat/One", "fix/two", "feat/three"):
            branch = Mock()
            branch.name = name
            branches.append(branch)
        mock_repo.get_branches.return_value = iter(branches)
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.list_branches("owner/repo", search="feat")

        assert result == ["Feat/One", "feat/three"]

    def test_list_branches_respects_limit_and_stops_iteration(self, github_client):
        """Iteration stops once `limit` matches are collected — no full page scan."""
        branches = []
        for name in ("a", "b", "c", "d"):
            branch = Mock()
            branch.name = name
            branches.append(branch)
        mock_repo = Mock()
        mock_repo.get_branches.return_value = iter(branches)
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.list_branches("owner/repo", limit=2)

        assert result == ["a", "b"]

    def test_get_merge_request_by_branches_returns_first_open_match(self, github_client):
        """When an open PR exists for the source/target pair, return a serialized MergeRequest."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.number = 7
        mock_pr.head = Mock(ref="feat-x", sha="abc123")
        mock_pr.base = Mock(ref="main")
        mock_pr.title = "feat: add x"
        mock_pr.body = "details"
        label = Mock()
        label.name = "enhancement"
        mock_pr.labels = [label]
        mock_pr.html_url = "https://github.com/o/r/pull/7"
        mock_user = Mock(id=1, login="alice")
        mock_user.name = "Alice"
        mock_pr.user = mock_user
        mock_pr.draft = True
        mock_repo.get_pulls.return_value = iter([mock_pr])
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.get_merge_request_by_branches("owner/repo", "feat-x", "main")

        assert result is not None
        assert result.merge_request_id == 7
        assert result.source_branch == "feat-x"
        assert result.target_branch == "main"
        assert result.draft is True
        assert result.web_url == "https://github.com/o/r/pull/7"
        assert result.labels == ["enhancement"]
        mock_repo.get_pulls.assert_called_once_with(state="open", base="main", head="feat-x")

    def test_get_merge_request_by_branches_returns_none_when_empty(self, github_client):
        """No open PR matching the branch pair → ``None``."""
        mock_repo = Mock()
        mock_repo.get_pulls.return_value = iter([])
        github_client.client.get_repo.return_value = mock_repo

        result = github_client.get_merge_request_by_branches("owner/repo", "feat-x", "main")

        assert result is None
