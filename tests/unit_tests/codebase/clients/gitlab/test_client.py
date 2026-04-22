from unittest.mock import Mock, patch

import pytest
from gitlab.exceptions import GitlabGetError

from codebase.base import GitPlatform, MergeRequestCommit, MergeRequestDiffStats, Repository, User
from codebase.clients.base import Emoji
from codebase.clients.gitlab.client import GitLabClient

_POSITION = {
    "position_type": "text",
    "base_sha": "aaa",
    "start_sha": "bbb",
    "head_sha": "ccc",
    "old_path": "src/foo.py",
    "new_path": "src/foo.py",
    "new_line": 42,
}


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
            html_url="https://gitlab.com/group/repo",
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

    def test_create_merge_request_inline_discussion_sends_position_payload(self, gitlab_client):
        """create_merge_request_inline_discussion must pass body + position dict to discussions.create."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_discussion = Mock()
        mock_discussion.id = "disc-abc"
        mock_project.mergerequests.get.return_value = mock_mr
        mock_mr.discussions.create.return_value = mock_discussion
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.create_merge_request_inline_discussion(
            repo_id="group/repo", merge_request_id=5, body="This looks wrong.", position=_POSITION
        )

        assert result == "disc-abc"
        mock_project.mergerequests.get.assert_called_once_with(5, lazy=True)
        mock_mr.discussions.create.assert_called_once_with({"body": "This looks wrong.", "position": _POSITION})

    def test_create_merge_request_inline_discussion_returns_discussion_id(self, gitlab_client):
        """The returned value must be the discussion ID string from GitLab."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_discussion = Mock()
        mock_discussion.id = "unique-id-xyz"
        mock_project.mergerequests.get.return_value = mock_mr
        mock_mr.discussions.create.return_value = mock_discussion
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.create_merge_request_inline_discussion("ns/proj", 99, "body text", _POSITION)

        assert result == "unique-id-xyz"

    def test_get_merge_request_diff_stats_standard_diff(self, gitlab_client):
        """Test diff stats parsing with a standard unified diff."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_mr.changes.return_value = {
            "changes": [
                {
                    "diff": (
                        "@@ -1,3 +1,4 @@\n"
                        " unchanged line\n"
                        "-removed line\n"
                        "+added line 1\n"
                        "+added line 2\n"
                        " another unchanged\n"
                    )
                }
            ]
        }
        mock_project.mergerequests.get.return_value = mock_mr
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_diff_stats("group/repo", 1)

        assert result == MergeRequestDiffStats(lines_added=2, lines_removed=1, files_changed=1)

    def test_get_merge_request_diff_stats_excludes_diff_headers(self, gitlab_client):
        """Test that +++ and --- diff headers are not counted as added/removed lines."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_mr.changes.return_value = {
            "changes": [{"diff": ("--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-old\n+new\n")}]
        }
        mock_project.mergerequests.get.return_value = mock_mr
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_diff_stats("group/repo", 1)

        assert result.lines_added == 1
        assert result.lines_removed == 1

    def test_get_merge_request_diff_stats_multiple_files(self, gitlab_client):
        """Test diff stats across multiple changed files."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_mr.changes.return_value = {
            "changes": [{"diff": "@@ -1 +1 @@\n-a\n+b\n"}, {"diff": "@@ -1 +1,3 @@\n-x\n+y\n+z\n+w\n"}]
        }
        mock_project.mergerequests.get.return_value = mock_mr
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_diff_stats("group/repo", 1)

        assert result.lines_added == 4
        assert result.lines_removed == 2
        assert result.files_changed == 2

    def test_get_merge_request_diff_stats_empty_diff(self, gitlab_client):
        """Test diff stats when a file change has an empty diff (e.g., binary file)."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_mr.changes.return_value = {"changes": [{"diff": ""}]}
        mock_project.mergerequests.get.return_value = mock_mr
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_diff_stats("group/repo", 1)

        assert result.lines_added == 0
        assert result.lines_removed == 0
        assert result.files_changed == 1

    def test_get_merge_request_diff_stats_no_changes(self, gitlab_client):
        """Test diff stats with no file changes."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_mr.changes.return_value = {"changes": []}
        mock_project.mergerequests.get.return_value = mock_mr
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_diff_stats("group/repo", 1)

        assert result == MergeRequestDiffStats(lines_added=0, lines_removed=0, files_changed=0)

    def test_get_merge_request_diff_stats_overflow_still_returns_partial(self, gitlab_client):
        """Test that overflow flag logs a warning but still returns partial stats."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_mr.changes.return_value = {"overflow": True, "changes": [{"diff": "@@ -1 +1 @@\n-old\n+new\n"}]}
        mock_project.mergerequests.get.return_value = mock_mr
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_diff_stats("group/repo", 1)

        assert result.lines_added == 1
        assert result.lines_removed == 1
        assert result.files_changed == 1

    def test_get_merge_request_diff_stats_only_additions(self, gitlab_client):
        """Test diff stats for a new file with only additions."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_mr.changes.return_value = {"changes": [{"diff": "@@ -0,0 +1,3 @@\n+line1\n+line2\n+line3\n"}]}
        mock_project.mergerequests.get.return_value = mock_mr
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_diff_stats("group/repo", 1)

        assert result.lines_added == 3
        assert result.lines_removed == 0

    def test_get_merge_request_commits_returns_commit_list(self, gitlab_client):
        """Test that commits are returned with author email and stats."""
        mock_project = Mock()
        mock_mr = Mock()
        commit_ref = Mock(id="abc123", author_email="dev@example.com")
        mock_mr.commits.return_value = [commit_ref]
        full_commit = Mock(stats={"additions": 10, "deletions": 5})
        mock_project.mergerequests.get.return_value = mock_mr
        mock_project.commits.get.return_value = full_commit
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_commits("group/repo", 1)

        assert result == [
            MergeRequestCommit(sha="abc123", author_email="dev@example.com", lines_added=10, lines_removed=5)
        ]

    def test_get_merge_request_commits_caps_at_100(self, gitlab_client):
        """Test that commits are capped at 100."""
        mock_project = Mock()
        mock_mr = Mock()
        commits = [Mock(id=f"sha{i}", author_email="dev@example.com") for i in range(150)]
        mock_mr.commits.return_value = commits
        mock_project.mergerequests.get.return_value = mock_mr
        mock_project.commits.get.return_value = Mock(stats={"additions": 1, "deletions": 0})
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_commits("group/repo", 1)

        assert len(result) == 100

    def test_get_merge_request_commits_handles_none_stats(self, gitlab_client):
        """Test that None stats are treated as zero."""
        mock_project = Mock()
        mock_mr = Mock()
        commit_ref = Mock(id="abc123", author_email="dev@example.com")
        mock_mr.commits.return_value = [commit_ref]
        mock_project.mergerequests.get.return_value = mock_mr
        mock_project.commits.get.return_value = Mock(stats=None)
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_commits("group/repo", 1)

        assert result[0].lines_added == 0
        assert result[0].lines_removed == 0

    def test_get_merge_request_commits_handles_none_author_email(self, gitlab_client):
        """Test that None author_email defaults to empty string."""
        mock_project = Mock()
        mock_mr = Mock()
        commit_ref = Mock(id="abc123", author_email=None)
        mock_mr.commits.return_value = [commit_ref]
        mock_project.mergerequests.get.return_value = mock_mr
        mock_project.commits.get.return_value = Mock(stats={"additions": 1, "deletions": 0})
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_commits("group/repo", 1)

        assert result[0].author_email == ""

    def test_get_merge_request_commits_per_commit_error_continues(self, gitlab_client):
        """Test that a per-commit API failure skips stats but still includes the commit."""
        mock_project = Mock()
        mock_mr = Mock()
        commit_ref1 = Mock(id="abc", author_email="dev@example.com")
        commit_ref2 = Mock(id="def", author_email="dev@example.com")
        mock_mr.commits.return_value = [commit_ref1, commit_ref2]
        mock_project.mergerequests.get.return_value = mock_mr
        mock_project.commits.get.side_effect = [GitlabGetError("404"), Mock(stats={"additions": 5, "deletions": 3})]
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_commits("group/repo", 1)

        assert len(result) == 2
        assert result[0].lines_added == 0  # failed commit gets zero stats
        assert result[1].lines_added == 5

    def test_list_branches_passes_search_and_per_page(self, gitlab_client):
        """`list_branches` forwards `search` and caps `per_page` at `limit`."""
        mock_project = Mock()
        mock_branch = Mock()
        mock_branch.name = "feat/one"
        mock_project.branches.list.return_value = iter([mock_branch])
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.list_branches("group/repo", search="feat", limit=10)

        assert result == ["feat/one"]
        gitlab_client.client.projects.get.assert_called_once_with("group/repo", lazy=True)
        mock_project.branches.list.assert_called_once_with(iterator=True, per_page=10, search="feat")

    def test_list_branches_without_search_omits_search_kwarg(self, gitlab_client):
        """Omitting `search` means no `search=` is passed to GitLab (returns all)."""
        mock_project = Mock()
        mock_project.branches.list.return_value = iter([])
        gitlab_client.client.projects.get.return_value = mock_project

        gitlab_client.list_branches("group/repo")

        mock_project.branches.list.assert_called_once_with(iterator=True, per_page=20)

    def test_list_branches_respects_limit(self, gitlab_client):
        """Iterator yields more than `limit`; result is truncated to `limit`."""
        mock_project = Mock()
        branches = []
        for name in ("a", "b", "c", "d"):
            branch = Mock()
            branch.name = name
            branches.append(branch)
        mock_project.branches.list.return_value = iter(branches)
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.list_branches("group/repo", limit=2)

        assert result == ["a", "b"]

    def test_list_branches_caps_per_page_at_100(self, gitlab_client):
        """GitLab's per_page maximum is 100; values above are clamped."""
        mock_project = Mock()
        mock_project.branches.list.return_value = iter([])
        gitlab_client.client.projects.get.return_value = mock_project

        gitlab_client.list_branches("group/repo", limit=500)

        _, kwargs = mock_project.branches.list.call_args
        assert kwargs["per_page"] == 100
