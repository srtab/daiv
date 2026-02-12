from unittest.mock import Mock, patch

import pytest

from codebase.base import GitPlatform, Repository, User
from codebase.clients.base import Emoji
from codebase.clients.gitlab.client import GitLabClient


class TestGitLabClient:
    """Tests for GitLabClient."""

    @pytest.fixture
    def gitlab_client(self):
        """Create a GitLabClient instance with mocked dependencies."""
        from unittest.mock import patch

        mock_gitlab = Mock()
        with patch("codebase.clients.gitlab.client.Gitlab", return_value=mock_gitlab):
            client = GitLabClient(auth_token="test-token", url="https://gitlab.com")  # noqa: S106
            yield client

    @pytest.mark.parametrize(
        ("award_emojis", "emoji", "expected"),
        [
            pytest.param([("eyes", 456), ("eyes", 123)], Emoji.EYES, True, id="reaction-exists-for-current-user"),
            pytest.param([("eyes", 456)], Emoji.EYES, False, id="reaction-from-different-user"),
            pytest.param([("thumbsup", 123)], Emoji.EYES, False, id="different-emoji"),
            pytest.param([], Emoji.EYES, False, id="no-reactions"),
        ],
    )
    def test_has_issue_reaction(self, gitlab_client, monkeypatch, award_emojis, emoji, expected):
        """Test issue award emoji matching for user and emoji combinations."""
        mock_project = Mock()
        mock_issue = Mock()
        mock_reactions = []
        for name, user_id in award_emojis:
            award_emoji = Mock()
            award_emoji.name = name
            award_emoji.user = {"id": user_id}
            mock_reactions.append(award_emoji)

        monkeypatch.setattr(type(gitlab_client), "current_user", User(id=123, username="daiv", name="DAIV"))
        gitlab_client.client.projects.get.return_value = mock_project
        mock_project.issues.get.return_value = mock_issue
        mock_issue.awardemojis.list.return_value = mock_reactions

        result = gitlab_client.has_issue_reaction("group/repo", 123, emoji)
        assert result is expected

    @patch("codebase.clients.gitlab.client.Repo.clone_from")
    def test_load_repo_configures_git_identity_with_gitlab_user(self, mock_clone_from, gitlab_client, monkeypatch):
        """Test load_repo configures local git identity to the GitLab user."""
        mock_repo = Mock()
        mock_writer = Mock()
        mock_repo.config_writer.return_value.__enter__ = Mock(return_value=mock_writer)
        mock_repo.config_writer.return_value.__exit__ = Mock(return_value=None)
        mock_clone_from.return_value = mock_repo

        gitlab_client.client.private_token = "token"  # noqa: S105
        gitlab_client.client.user = Mock(
            username="daiv-agent-test", public_email="daiv-agent-test@users.noreply.gitlab.com"
        )
        gitlab_client.client.auth = Mock()
        monkeypatch.setattr(
            type(gitlab_client), "current_user", User(id=123456, username="daiv-agent-test", name="DAIV Agent Test")
        )

        repository = Repository(
            pk=1,
            slug="group/repo",
            name="repo",
            clone_url="https://gitlab.com/group/repo.git",
            default_branch="main",
            git_platform=GitPlatform.GITLAB,
        )

        with gitlab_client.load_repo(repository, "main") as loaded_repo:
            assert loaded_repo == mock_repo

        clone_url, clone_dir = mock_clone_from.call_args.args[:2]
        branch = mock_clone_from.call_args.kwargs["branch"]
        assert clone_url == "https://oauth2:token@gitlab.com/group/repo.git"
        assert clone_dir.name == "repo"
        assert branch == "main"
        mock_writer.set_value.assert_any_call("user", "name", "daiv-agent-test")
        mock_writer.set_value.assert_any_call("user", "email", "daiv-agent-test@users.noreply.gitlab.com")
