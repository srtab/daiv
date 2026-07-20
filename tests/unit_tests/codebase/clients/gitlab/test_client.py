import logging
from unittest.mock import Mock, call, patch

from django.core.exceptions import ImproperlyConfigured

import pytest
from git import GitCommandError
from gitlab.exceptions import GitlabCreateError, GitlabGetError

from codebase.base import GitPlatform, MergeRequestCommit, MergeRequestDiffStats, Repository, User
from codebase.clients.base import Emoji, GitAuthEnv
from codebase.clients.gitlab.client import (
    MERGE_REQUEST_BRANCH_VISIBILITY_RETRY_BACKOFF_SECONDS,
    GitLabClient,
    _is_source_branch_missing_error,
)

_CLONE_URL = "https://gitlab.com/group/repo.git"


def _expected_clone_env(token: str) -> dict[str, str]:
    """The git env the clone/publish is expected to run with for ``token`` (fixture's clone_url)."""
    return GitAuthEnv.for_token(_CLONE_URL, token).as_env()


def _gl_project(slug: str) -> Mock:
    """A mock GitLab project shaped like what ``projects.list`` yields."""
    project = Mock()
    project.get_id.return_value = 1
    project.path_with_namespace = slug
    project.name = slug.split("/")[-1]
    project.web_url = f"https://gitlab.com/{slug}"
    project.default_branch = "main"
    project.topics = []
    return project


def test_git_egress_credential_for_token_builds_basic_oauth2_header():
    import base64

    from codebase.clients.base import GitEgressCredential

    cred = GitEgressCredential.for_token(host="gitlab.example.com", token="tok-123")  # noqa: S106
    assert cred.host == "gitlab.example.com"
    assert cred.header == "Authorization"
    assert cred.value.get_secret_value() == "Basic " + base64.b64encode(b"oauth2:tok-123").decode()


def test_git_egress_credential_for_token_none_when_no_token():
    from codebase.clients.base import GitEgressCredential

    cred = GitEgressCredential.for_token(host="gitlab.example.com", token=None)
    assert cred.host == "gitlab.example.com"
    assert cred.value is None


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

    @pytest.fixture
    def clone_setup(self, gitlab_client, monkeypatch):
        """Patched clone environment: yields (repository, clone_from mock, config writer mock)."""
        mock_repo = Mock()
        mock_writer = Mock()
        mock_repo.config_writer.return_value.__enter__ = Mock(return_value=mock_writer)
        mock_repo.config_writer.return_value.__exit__ = Mock(return_value=None)

        gitlab_client.client.private_token = "pat-token"  # noqa: S105
        gitlab_client.client.user = Mock(username="daiv", public_email="daiv@users.noreply.gitlab.com")
        gitlab_client.client.auth = Mock()
        monkeypatch.setattr(type(gitlab_client), "current_user", User(id=1, username="daiv", name="DAIV"))

        repository = Repository(
            pk=1,
            slug="group/repo",
            name="repo",
            clone_url="https://gitlab.com/group/repo.git",
            html_url="https://gitlab.com/group/repo",
            default_branch="main",
            git_platform=GitPlatform.GITLAB,
        )
        with patch("codebase.clients.gitlab.client.Repo.clone_from", return_value=mock_repo) as clone_from:
            yield repository, clone_from, mock_writer

    def test_load_repo_configures_git_identity_with_gitlab_user(self, gitlab_client, clone_setup):
        """Test load_repo configures local git identity to the GitLab user."""
        repository, clone_from, mock_writer = clone_setup
        mock_repo = clone_from.return_value

        with (
            patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value=None),
            gitlab_client.load_repo(repository, "main") as loaded_repo,
        ):
            assert loaded_repo == mock_repo

        clone_url, clone_dir = clone_from.call_args.args[:2]
        branch = clone_from.call_args.kwargs["branch"]
        assert clone_url == "https://gitlab.com/group/repo.git"
        assert clone_from.call_args.kwargs["env"] == _expected_clone_env("pat-token")
        assert clone_dir.name == "repo"
        assert branch == "main"
        mock_writer.set_value.assert_any_call("user", "name", "daiv")
        mock_writer.set_value.assert_any_call("user", "email", "daiv@users.noreply.gitlab.com")

    def test_load_repo_prefers_ephemeral_token(self, gitlab_client, clone_setup):
        """When provisioning succeeds, the ephemeral token authenticates the clone, not the PAT —
        and only via the command-scoped env, never the URL (which would persist it in .git/config)."""
        repository, clone_from, _ = clone_setup

        with (
            patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value="glpat-eph") as get_token,
            gitlab_client.load_repo(repository, "main"),
        ):
            pass

        get_token.assert_called_once_with(gitlab_client.client, 1)
        assert clone_from.call_args.args[0] == "https://gitlab.com/group/repo.git"
        assert clone_from.call_args.kwargs["env"] == _expected_clone_env("glpat-eph")

    def test_load_repo_falls_back_to_pat_when_provisioning_fails(self, gitlab_client, clone_setup, caplog):
        """When provisioning returns None (tier/role/API failure), the PAT keeps clones working.

        The fallback authenticates with the full-API PAT, so each clone taking it must say so
        in its own log stream (the mint-time warning fires once per hour on one worker only).
        """
        repository, clone_from, _ = clone_setup

        with (
            patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value=None),
            caplog.at_level(logging.INFO, logger="daiv.clients"),
            gitlab_client.load_repo(repository, "main"),
        ):
            pass

        assert clone_from.call_args.args[0] == "https://gitlab.com/group/repo.git"
        assert clone_from.call_args.kwargs["env"] == _expected_clone_env("pat-token")
        assert "with the configured PAT" in caplog.text

    def test_load_repo_raises_when_pat_is_missing(self, gitlab_client, clone_setup):
        """No ephemeral token and no PAT must fail with a named config error, not an oauth2:None@ URL."""
        repository, clone_from, _ = clone_setup
        gitlab_client.client.private_token = None

        with (
            patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value=None),
            pytest.raises(ImproperlyConfigured),
            gitlab_client.load_repo(repository, "main"),
        ):
            pass

        clone_from.assert_not_called()

    def test_load_repo_retries_with_fresh_token_when_cached_ephemeral_is_rejected(self, gitlab_client, clone_setup):
        """A cached ephemeral token GitLab now rejects must be dropped and re-minted, not left to
        wedge every clone of this project for the rest of the cache window."""
        repository, clone_from, _ = clone_setup
        mock_repo = clone_from.return_value
        auth_error = GitCommandError(
            "git clone", 128, "remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://...'"
        )
        clone_from.side_effect = [auth_error, mock_repo]

        with (
            patch(
                "codebase.clients.gitlab.client.get_ephemeral_clone_token", side_effect=["glpat-stale", "glpat-fresh"]
            ),
            patch("codebase.clients.gitlab.client.invalidate_clone_token") as invalidate,
            gitlab_client.load_repo(repository, "main") as loaded_repo,
        ):
            assert loaded_repo == mock_repo

        invalidate.assert_called_once_with(1)
        assert clone_from.call_count == 2
        first_env = clone_from.call_args_list[0].kwargs["env"]
        second_env = clone_from.call_args_list[1].kwargs["env"]
        assert first_env == _expected_clone_env("glpat-stale")
        assert second_env == _expected_clone_env("glpat-fresh")

    def test_load_repo_retries_exactly_once_then_propagates_when_fresh_token_also_rejected(
        self, gitlab_client, clone_setup
    ):
        """The self-heal retries at most once: if the freshly minted token is also rejected, the
        second failure propagates rather than looping. Pins 'retry once, then give up'."""
        repository, clone_from, _ = clone_setup
        auth_error = GitCommandError(
            "git clone", 128, "remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://...'"
        )
        clone_from.side_effect = [auth_error, auth_error]

        with (
            patch(
                "codebase.clients.gitlab.client.get_ephemeral_clone_token", side_effect=["glpat-stale", "glpat-fresh"]
            ),
            patch("codebase.clients.gitlab.client.invalidate_clone_token") as invalidate,
            pytest.raises(GitCommandError),
            gitlab_client.load_repo(repository, "main"),
        ):
            pass

        invalidate.assert_called_once_with(1)
        assert clone_from.call_count == 2

    def test_load_repo_does_not_retry_when_the_clone_used_the_pat(self, gitlab_client, clone_setup):
        """Re-minting can't fix a clone that already used the PAT directly (no ephemeral token to
        evict), so an auth-rejected PAT clone must surface immediately, not retry."""
        repository, clone_from, _ = clone_setup
        clone_from.side_effect = GitCommandError("git clone", 128, "fatal: Authentication failed for 'https://...'")

        with (
            patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value=None),
            patch("codebase.clients.gitlab.client.invalidate_clone_token") as invalidate,
            pytest.raises(GitCommandError),
            gitlab_client.load_repo(repository, "main"),
        ):
            pass

        invalidate.assert_not_called()
        assert clone_from.call_count == 1

    def test_get_git_auth_env_reuses_clone_token(self, gitlab_client, clone_setup):
        """Local-mode git (sandbox-disabled publishes) authenticates each invocation with the same
        short-lived clone token the clone used — nothing is persisted in the clone itself."""
        repository, _, _ = clone_setup

        with patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value="glpat-eph"):
            auth_env = gitlab_client.get_git_auth_env(repository)

        assert auth_env == GitAuthEnv.for_token(_CLONE_URL, "glpat-eph")

    def test_get_git_auth_env_none_when_no_token(self, gitlab_client, clone_setup, caplog):
        """No token at all (no ephemeral, no PAT) means nothing to authenticate with — callers
        get None instead of a bogus header, and a debug line records it so a provisioning failure
        is distinguishable from a genuinely credential-less (public-repo) platform."""
        repository, _, _ = clone_setup
        gitlab_client.client.private_token = None

        with (
            patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value=None),
            caplog.at_level(logging.DEBUG, logger="daiv.clients"),
        ):
            assert gitlab_client.get_git_auth_env(repository) is None

        assert "No git credential resolved for group/repo" in caplog.text

    def test_load_repo_does_not_retry_non_auth_clone_failures(self, gitlab_client, clone_setup):
        """A missing branch (or any non-auth 128) won't be fixed by a fresh credential, so it must
        not trigger the token-rotation retry."""
        repository, clone_from, _ = clone_setup
        clone_from.side_effect = GitCommandError(
            "git clone", 128, "fatal: Remote branch nope not found in upstream origin"
        )

        with (
            patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value="glpat-eph"),
            patch("codebase.clients.gitlab.client.invalidate_clone_token") as invalidate,
            pytest.raises(GitCommandError),
            gitlab_client.load_repo(repository, "main"),
        ):
            pass

        invalidate.assert_not_called()
        assert clone_from.call_count == 1

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
        mock_mr.discussions.create.assert_called_once_with(
            {"body": "This looks wrong.", "position": _POSITION}, retry_transient_errors=False
        )

    def test_create_merge_request_inline_discussion_disables_transient_retry(self, gitlab_client):
        """The create POST must disable python-gitlab's transient-error retry.

        The client-level ``retry_transient_errors=True`` retries 5xx with backoff; GitLab can 500
        *after* persisting the note, so retrying a non-idempotent discussion-create POST duplicates
        the comment (observed in production as one inline finding posted 12×). This write must opt out.
        """
        mock_project = Mock()
        mock_mr = Mock()
        mock_discussion = Mock()
        mock_discussion.id = "disc-abc"
        mock_project.mergerequests.get.return_value = mock_mr
        mock_mr.discussions.create.return_value = mock_discussion
        gitlab_client.client.projects.get.return_value = mock_project

        gitlab_client.create_merge_request_inline_discussion(
            repo_id="group/repo", merge_request_id=5, body="This looks wrong.", position=_POSITION
        )

        _, kwargs = mock_mr.discussions.create.call_args
        assert kwargs.get("retry_transient_errors") is False

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

    @staticmethod
    def _merge_request_mock(source_branch="feat/x"):
        """A GitLab merge request object shaped for `_serialize_merge_request`."""
        mock_mr = Mock()
        mock_mr.get_id.return_value = 42
        mock_mr.source_branch = source_branch
        mock_mr.target_branch = "main"
        mock_mr.title = "Title"
        mock_mr.description = "Desc"
        mock_mr.labels = ["daiv"]
        mock_mr.web_url = "https://gitlab.com/group/repo/-/merge_requests/42"
        mock_mr.sha = "deadbeef"
        mock_mr.author = {"id": 1, "username": "daiv", "name": "DAIV"}
        mock_mr.work_in_progress = False
        return mock_mr

    @staticmethod
    def _branch_missing_error():
        """The 400 GitLab raises when a just-pushed branch isn't visible to MR-create yet."""
        return GitlabCreateError(error_message={"source_branch": ["does not exist"]}, response_code=400)

    def test_update_or_create_merge_request_retries_past_branch_visibility_race(self, gitlab_client):
        """A just-pushed branch can be briefly invisible to MR-create; retry on the documented backoff until it lands.

        Drives a failure for every backoff step then a success, so it pins both that the eventual MR is
        returned/serialized and that the waits follow the exact schedule the module comment promises.
        """
        backoff = MERGE_REQUEST_BRANCH_VISIBILITY_RETRY_BACKOFF_SECONDS
        mock_project = Mock()
        mock_project.mergerequests.create.side_effect = [
            *(self._branch_missing_error() for _ in backoff),
            self._merge_request_mock(source_branch="feat/x"),
        ]
        gitlab_client.client.projects.get.return_value = mock_project

        with patch("codebase.clients.gitlab.client.time.sleep") as mock_sleep:
            result = gitlab_client.update_or_create_merge_request(
                repo_id="group/repo", source_branch="feat/x", target_branch="main", title="Title", description="Desc"
            )

        assert result.source_branch == "feat/x"
        assert mock_sleep.call_args_list == [call(delay) for delay in backoff]

    def test_update_or_create_merge_request_raises_when_branch_never_appears(self, gitlab_client):
        """If the branch stays invisible past the retry budget, the create error surfaces."""
        mock_project = Mock()
        mock_project.mergerequests.create.side_effect = self._branch_missing_error()
        gitlab_client.client.projects.get.return_value = mock_project

        with patch("codebase.clients.gitlab.client.time.sleep"), pytest.raises(GitlabCreateError):
            gitlab_client.update_or_create_merge_request(
                repo_id="group/repo", source_branch="feat/x", target_branch="main", title="Title", description="Desc"
            )

        # One attempt per backoff step, plus the final attempt after the retries are exhausted.
        expected_attempts = len(MERGE_REQUEST_BRANCH_VISIBILITY_RETRY_BACKOFF_SECONDS) + 1
        assert mock_project.mergerequests.create.call_count == expected_attempts

    def test_update_or_create_merge_request_does_not_retry_unrelated_400(self, gitlab_client):
        """A 400 that isn't the branch-visibility race must fail fast (no retry/sleep)."""
        mock_project = Mock()
        mock_project.mergerequests.create.side_effect = GitlabCreateError(
            error_message={"title": ["can't be blank"]}, response_code=400
        )
        gitlab_client.client.projects.get.return_value = mock_project

        with patch("codebase.clients.gitlab.client.time.sleep") as mock_sleep, pytest.raises(GitlabCreateError):
            gitlab_client.update_or_create_merge_request(
                repo_id="group/repo", source_branch="feat/x", target_branch="main", title="Title", description="Desc"
            )

        mock_project.mergerequests.create.assert_called_once()
        mock_sleep.assert_not_called()

    def test_update_or_create_merge_request_updates_existing_on_conflict(self, gitlab_client):
        """A 409 means an MR already exists for this branch pair -> update it in place, no retry."""
        mock_project = Mock()
        mock_project.mergerequests.create.side_effect = GitlabCreateError(
            error_message="Another open merge request already exists", response_code=409
        )
        existing_mr = self._merge_request_mock(source_branch="feat/x")
        mock_iterator = Mock()
        mock_iterator.next.return_value = existing_mr
        mock_project.mergerequests.list.return_value = mock_iterator
        gitlab_client.client.projects.get.return_value = mock_project

        with patch("codebase.clients.gitlab.client.time.sleep") as mock_sleep:
            result = gitlab_client.update_or_create_merge_request(
                repo_id="group/repo",
                source_branch="feat/x",
                target_branch="main",
                title="New Title",
                description="New Desc",
                labels=["daiv"],
            )

        assert result.source_branch == "feat/x"
        existing_mr.save.assert_called_once()
        mock_project.mergerequests.create.assert_called_once()
        mock_sleep.assert_not_called()

    @pytest.mark.parametrize(
        ("error_message", "response_code", "expected"),
        [
            # The real post-push race: GitLab surfaces the parsed body as a dict.
            pytest.param({"source_branch": ["does not exist"]}, 400, True, id="dict-body-is-the-race"),
            # python-gitlab can also surface the body as a str; the docstring promises this works.
            pytest.param("{'source_branch': ['does not exist']}", 400, True, id="str-body-is-the-race"),
            # Non-400 codes short-circuit before the body is inspected (409 must reach the update path).
            pytest.param({"source_branch": ["does not exist"]}, 409, False, id="conflict-short-circuits"),
            pytest.param({"source_branch": ["does not exist"]}, 404, False, id="not-found-short-circuits"),
            # Unrelated 400s must not retry.
            pytest.param({"title": ["can't be blank"]}, 400, False, id="unrelated-400"),
            # Both substrings are required: mentioning the field without "does not exist" must not fire...
            pytest.param({"source_branch": ["already exists"]}, 400, False, id="source-branch-but-not-missing"),
            # ...nor "does not exist" without naming source_branch.
            pytest.param("target_branch does not exist", 400, False, id="missing-but-not-source-branch"),
        ],
    )
    def test_is_source_branch_missing_error(self, error_message, response_code, expected):
        """The retry predicate fires only on a 400 whose body names source_branch AND says it's missing."""
        error = GitlabCreateError(error_message=error_message, response_code=response_code)
        assert _is_source_branch_missing_error(error) is expected

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

    def test_list_repositories_orders_by_last_activity_desc(self, gitlab_client):
        """`list_repositories` passes ``order_by=last_activity_at, sort=desc`` so recent projects surface first."""
        gitlab_client.client.projects.list.return_value = iter([])

        gitlab_client.list_repositories()

        _, kwargs = gitlab_client.client.projects.list.call_args
        assert kwargs["order_by"] == "last_activity_at"
        assert kwargs["sort"] == "desc"

    @pytest.mark.parametrize(
        ("slugs", "limit", "expected"),
        [
            # A listing ordered by a mutable key (last_activity_at) can surface the same project on
            # two pages, so `list_repositories` returns one entry per slug.
            pytest.param(["g/a", "g/b", "g/a"], None, ["g/a", "g/b"], id="dedupes-repeated-slugs"),
            # `limit` bounds unique repos, not raw rows: a duplicate must not consume a slot.
            pytest.param(["g/a", "g/a", "g/b"], 2, ["g/a", "g/b"], id="limit-counts-unique-slugs"),
        ],
    )
    def test_list_repositories_dedupes_by_slug(self, gitlab_client, slugs, limit, expected):
        gitlab_client.client.projects.list.return_value = iter([_gl_project(s) for s in slugs])

        assert [r.slug for r in gitlab_client.list_repositories(limit=limit)] == expected

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
        mock_project.branches.list.assert_called_once_with(
            iterator=True, per_page=10, sort="updated_desc", search="feat"
        )

    def test_list_branches_without_search_omits_search_kwarg(self, gitlab_client):
        """Omitting `search` means no `search=` is passed to GitLab (returns all)."""
        mock_project = Mock()
        mock_project.branches.list.return_value = iter([])
        gitlab_client.client.projects.get.return_value = mock_project

        gitlab_client.list_branches("group/repo")

        mock_project.branches.list.assert_called_once_with(iterator=True, per_page=20, sort="updated_desc")

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

    def test_get_merge_request_by_branches_returns_first_open_match(self, gitlab_client):
        """When an open MR exists for the source/target pair, return the serialized MR."""
        mock_project = Mock()
        mock_mr = Mock()
        mock_project.mergerequests.list.return_value = iter([mock_mr])
        gitlab_client.client.projects.get.return_value = mock_project
        sentinel = Mock(name="serialized")
        with patch.object(gitlab_client, "_serialize_merge_request", return_value=sentinel) as serialize:
            result = gitlab_client.get_merge_request_by_branches("group/repo", "feat-x", "main")

        assert result is sentinel
        mock_project.mergerequests.list.assert_called_once_with(
            source_branch="feat-x", target_branch="main", state="opened", iterator=True
        )
        serialize.assert_called_once_with("group/repo", mock_mr)

    def test_is_branch_protected_returns_true_when_branch_protected(self, gitlab_client):
        mock_project = Mock()
        mock_branch = Mock(protected=True)
        mock_project.branches.get.return_value = mock_branch
        gitlab_client.client.projects.get.return_value = mock_project

        assert gitlab_client.is_branch_protected("group/repo", "dev") is True
        mock_project.branches.get.assert_called_once_with("dev")

    def test_is_branch_protected_returns_false_when_branch_unprotected(self, gitlab_client):
        mock_project = Mock()
        mock_project.branches.get.return_value = Mock(protected=False)
        gitlab_client.client.projects.get.return_value = mock_project

        assert gitlab_client.is_branch_protected("group/repo", "feature") is False

    def test_is_branch_protected_returns_false_on_api_error(self, gitlab_client):
        """Fails open: treats any GitLab error (404, auth, rate limit, transport) as unprotected."""
        from gitlab.exceptions import GitlabAuthenticationError

        mock_project = Mock()
        gitlab_client.client.projects.get.return_value = mock_project

        for error in (GitlabGetError("404", response_code=404), GitlabAuthenticationError("401")):
            mock_project.branches.get.side_effect = error
            assert gitlab_client.is_branch_protected("group/repo", "missing") is False

    def test_get_merge_request_by_branches_returns_none_when_empty(self, gitlab_client):
        """Empty list → ``None`` (not an exception)."""
        mock_project = Mock()
        mock_project.mergerequests.list.return_value = iter([])
        gitlab_client.client.projects.get.return_value = mock_project

        result = gitlab_client.get_merge_request_by_branches("group/repo", "feat-x", "main")

        assert result is None

    def _egress_repo(self):
        return Repository(
            pk=42,
            slug="group/repo",
            name="repo",
            clone_url="https://gitlab.example.com/group/repo.git",
            html_url="https://gitlab.example.com/group/repo",
            default_branch="main",
            git_platform=GitPlatform.GITLAB,
        )

    def test_get_git_egress_credential_uses_ephemeral_clone_token(self, gitlab_client):
        import base64

        with patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value="glpat-eph"):
            cred = gitlab_client.get_git_egress_credential(self._egress_repo())
        assert cred.host == "gitlab.example.com"
        assert cred.header == "Authorization"
        assert cred.value.get_secret_value() == "Basic " + base64.b64encode(b"oauth2:glpat-eph").decode()

    def test_get_git_egress_credential_falls_back_to_pat(self, gitlab_client):
        import base64

        gitlab_client.client.private_token = "pat-token"  # noqa: S105
        with patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value=None):
            cred = gitlab_client.get_git_egress_credential(self._egress_repo())
        assert cred.value.get_secret_value() == "Basic " + base64.b64encode(b"oauth2:pat-token").decode()

    def test_get_git_egress_credential_no_token_returns_host_only(self, gitlab_client):
        gitlab_client.client.private_token = None
        with patch("codebase.clients.gitlab.client.get_ephemeral_clone_token", return_value=None):
            cred = gitlab_client.get_git_egress_credential(self._egress_repo())
        assert cred.host == "gitlab.example.com"
        assert cred.value is None
