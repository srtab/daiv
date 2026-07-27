from unittest.mock import Mock

import pytest
from git import GitCommandError

from codebase.clients.utils import _clean_ansi_codes, translate_missing_ref
from codebase.exceptions import RepositoryRefNotFoundError

_MISSING_BRANCH_STDERR = "fatal: Remote branch feature-x not found in upstream origin"


class TestTranslateMissingRef:
    """A clone whose ref is *confirmed* gone must surface as a typed, actionable error.

    The trigger case is a chat session pinned to a merged MR's auto-deleted source branch:
    without the translation the caller only sees an opaque ``GitCommandError`` and cannot tell
    "the branch vanished" (recoverable — retarget the default branch) from "git broke".

    git's wording alone is not enough evidence: the same message appears when a branch is merely
    absent from the advertisement this credential got (replication lag, restricted ref view,
    stale proxy). Since the chat fallback rewrites the session's stored ref, a false positive is
    destructive — so the platform API has to agree.
    """

    def _clone_error(self, stderr: str) -> GitCommandError:
        return GitCommandError(["git", "clone", "--branch=feature-x"], 128, stderr)

    def _client(self, exists: bool | None) -> Mock:
        client = Mock()
        client.branch_exists.return_value = exists
        return client

    def test_missing_remote_branch_confirmed_by_api_becomes_typed_error(self):
        client = self._client(exists=False)
        with (
            pytest.raises(RepositoryRefNotFoundError) as exc_info,
            translate_missing_ref(client, "group/project", "feature-x"),
        ):
            raise self._clone_error(_MISSING_BRANCH_STDERR)

        assert exc_info.value.ref == "feature-x"
        assert exc_info.value.repo_id == "group/project"
        # The cause is preserved so the server-side log keeps git's own wording.
        assert isinstance(exc_info.value.__cause__, GitCommandError)
        client.branch_exists.assert_called_once_with("group/project", "feature-x")

    def test_could_not_find_remote_branch_wording_also_matches(self):
        """Older/dumb-HTTP git reports the miss as a warning before failing."""
        with (
            pytest.raises(RepositoryRefNotFoundError),
            translate_missing_ref(self._client(exists=False), "group/project", "feature-x"),
        ):
            raise self._clone_error("warning: Could not find remote branch feature-x to clone.")

    def test_branch_still_present_on_the_api_keeps_the_raw_error(self):
        """git said missing but the platform says it exists → a transient advertisement miss.
        Must NOT be translated: the chat fallback would abandon a live branch and rewrite the
        session's ref with no way for the user to undo it."""
        with (
            pytest.raises(GitCommandError),
            translate_missing_ref(self._client(exists=True), "group/project", "feature-x"),
        ):
            raise self._clone_error(_MISSING_BRANCH_STDERR)

    def test_unknown_api_answer_keeps_the_raw_error(self):
        """``None`` = the platform could not be reached. An API outage must never be read as a
        deleted branch."""
        with (
            pytest.raises(GitCommandError),
            translate_missing_ref(self._client(exists=None), "group/project", "feature-x"),
        ):
            raise self._clone_error(_MISSING_BRANCH_STDERR)

    @pytest.mark.parametrize("ref", ["a1b2c3d", "0123456789abcdef0123456789abcdef01234567"])
    def test_commit_sha_ref_keeps_the_raw_error(self, ref):
        """``--branch`` resolves branches and tags but never a commit SHA, so the same message
        there means "that isn't a branch" — not "it was deleted". Callers may legitimately pin a
        SHA, and the API is never even consulted."""
        client = self._client(exists=False)
        with pytest.raises(GitCommandError), translate_missing_ref(client, "group/project", ref):
            raise self._clone_error(f"fatal: Remote branch {ref} not found in upstream origin")

        client.branch_exists.assert_not_called()

    def test_auth_failure_passes_through_untranslated(self):
        """Auth rejections have their own retry/self-heal path — never relabel them."""
        client = self._client(exists=False)
        with pytest.raises(GitCommandError), translate_missing_ref(client, "group/project", "feature-x"):
            raise self._clone_error("fatal: Authentication failed for 'https://git.example.com/x.git/'")

        # No wasted API call: the wording gate runs before the corroboration.
        client.branch_exists.assert_not_called()

    def test_unrelated_error_passes_through(self):
        with (
            pytest.raises(GitCommandError),
            translate_missing_ref(self._client(exists=False), "group/project", "feature-x"),
        ):
            raise self._clone_error("fatal: unable to access 'https://git.example.com/x.git/': Could not resolve host")

    def test_success_is_transparent(self):
        client = self._client(exists=False)
        with translate_missing_ref(client, "group/project", "feature-x"):
            pass

        client.branch_exists.assert_not_called()


def test__clean_ansi_codes():
    """Test that _clean_gitlab_logs properly cleans GitLab logs."""
    raw_log = (
        "\x1b[0msection_start:123: step_script\r\n"
        "Running command\x1b[0m\r\n"
        "Output with\rcarriage return\r\n"
        "\x1b[32mColored text\x1b[0m\n"
        "section_end:123: step_script"
    )

    result = _clean_ansi_codes(raw_log)

    assert "\x1b[" not in result  # No ANSI codes
    assert "\r\n" not in result  # No Windows line endings


def test__clean_ansi_codes_empty_log():
    """Test that _clean_ansi_codes handles empty logs."""
    assert _clean_ansi_codes("") == ""
