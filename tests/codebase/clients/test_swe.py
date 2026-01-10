from unittest.mock import Mock, patch

import pytest

from codebase.base import GitPlatform
from codebase.clients.swe import SWERepoClient


class TestSWERepoClient:
    """Tests for SWERepoClient."""

    @pytest.fixture
    def swe_client(self):
        """Create a SWERepoClient instance."""
        return SWERepoClient("github.com")

    def test_get_repository_success(self, swe_client):
        """Test successful repository creation."""
        repo = swe_client.get_repository("psf/requests")

        assert repo.slug == "psf/requests"
        assert repo.name == "requests"
        assert repo.clone_url == "https://github.com/psf/requests.git"
        assert repo.git_platform == GitPlatform.SWE
        assert repo.default_branch == "main"

    def test_get_repository_invalid_format(self, swe_client):
        """Test that invalid repo_id format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid repo_id format"):
            swe_client.get_repository("invalid-repo-id")

    def test_get_repository_custom_host(self):
        """Test repository creation with custom GitHub host."""
        client = SWERepoClient(repo_host="github.example.com")
        repo = client.get_repository("owner/repo")

        assert repo.clone_url == "https://github.example.com/owner/repo.git"

    def test_list_repositories_not_supported(self, swe_client):
        """Test that listing repositories raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="does not support listing repositories"):
            swe_client.list_repositories()

    @patch("codebase.clients.swe.Repo.clone_from")
    @patch("codebase.clients.swe.tempfile.TemporaryDirectory")
    def test_load_repo_success(self, mock_tempdir, mock_clone, swe_client):
        """Test successful repository loading."""
        mock_repo = Mock()
        mock_repo.git.checkout = Mock()
        mock_clone.return_value = mock_repo

        mock_tmpdir = "/tmp/test-repo"  # NOQA
        mock_tempdir.return_value.__enter__ = Mock(return_value=mock_tmpdir)
        mock_tempdir.return_value.__exit__ = Mock(return_value=None)

        repository = swe_client.get_repository("psf/requests")

        with swe_client.load_repo(repository, "abc123") as repo:
            assert repo == mock_repo
            mock_clone.assert_called_once_with("https://github.com/psf/requests.git", mock_tmpdir)
            mock_repo.git.checkout.assert_called_once_with("abc123")

    @patch("codebase.clients.swe.Repo.clone_from")
    @patch("codebase.clients.swe.tempfile.TemporaryDirectory")
    def test_get_repository_file_success(self, mock_tempdir, mock_clone, swe_client, tmp_path):
        """Test successful file retrieval."""
        mock_repo = Mock()
        mock_repo.working_dir = str(tmp_path)
        mock_clone.return_value = mock_repo

        mock_tmpdir = str(tmp_path)
        mock_tempdir.return_value.__enter__ = Mock(return_value=mock_tmpdir)
        mock_tempdir.return_value.__exit__ = Mock(return_value=None)

        # Create a mock file in the temp directory
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")

        repository = swe_client.get_repository("psf/requests")
        with swe_client.load_repo(repository, "main"):
            result = swe_client.get_repository_file("psf/requests", "test.py", "main")

        assert result == "print('hello')"

    @patch("codebase.clients.swe.Repo.clone_from")
    @patch("codebase.clients.swe.tempfile.TemporaryDirectory")
    def test_get_repository_file_not_found(self, mock_tempdir, mock_clone, swe_client, tmp_path):
        """Test file retrieval when file doesn't exist."""
        mock_repo = Mock()
        mock_repo.working_dir = str(tmp_path)
        mock_clone.return_value = mock_repo

        mock_tmpdir = str(tmp_path)
        mock_tempdir.return_value.__enter__ = Mock(return_value=mock_tmpdir)
        mock_tempdir.return_value.__exit__ = Mock(return_value=None)

        repository = swe_client.get_repository("psf/requests")
        with swe_client.load_repo(repository, "main"):
            result = swe_client.get_repository_file("psf/requests", "nonexistent.py", "main")

        assert result is None

    @patch("codebase.clients.swe.Repo.clone_from")
    @patch("codebase.clients.swe.tempfile.TemporaryDirectory")
    @patch("codebase.clients.swe.Path.read_text")
    def test_get_repository_file_binary(self, mock_read_text, mock_tempdir, mock_clone, swe_client, tmp_path):
        """Test file retrieval when file is binary."""
        mock_repo = Mock()
        mock_repo.working_dir = str(tmp_path)
        mock_clone.return_value = mock_repo

        mock_tmpdir = str(tmp_path)
        mock_tempdir.return_value.__enter__ = Mock(return_value=mock_tmpdir)
        mock_tempdir.return_value.__exit__ = Mock(return_value=None)

        # Create a file and mock read_text to raise UnicodeDecodeError
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"\x00\x01\x02")
        mock_read_text.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "invalid")

        repository = swe_client.get_repository("psf/requests")
        with swe_client.load_repo(repository, "main"):
            result = swe_client.get_repository_file("psf/requests", "test.bin", "main")

        assert result is None

    @patch("codebase.clients.swe.Repo.clone_from")
    @patch("codebase.clients.swe.tempfile.TemporaryDirectory")
    def test_get_project_uploaded_file_success(self, mock_tempdir, mock_clone, swe_client, tmp_path):
        """Test successful uploaded file retrieval."""
        mock_repo = Mock()
        mock_repo.working_dir = str(tmp_path)
        mock_clone.return_value = mock_repo

        mock_tmpdir = str(tmp_path)
        mock_tempdir.return_value.__enter__ = Mock(return_value=mock_tmpdir)
        mock_tempdir.return_value.__exit__ = Mock(return_value=None)

        # Create a mock file
        test_file = tmp_path / "image.png"
        test_file.write_bytes(b"image content")

        repository = swe_client.get_repository("psf/requests")
        with swe_client.load_repo(repository, "main"):
            result = swe_client.get_project_uploaded_file("psf/requests", "image.png")

        assert result == b"image content"

    @patch("codebase.clients.swe.Repo.clone_from")
    @patch("codebase.clients.swe.tempfile.TemporaryDirectory")
    def test_repository_branch_exists_true(self, mock_tempdir, mock_clone, swe_client):
        """Test branch existence check when branch exists."""
        mock_repo = Mock()
        mock_repo.git.fetch = Mock()
        mock_repo.git.checkout = Mock()
        mock_clone.return_value = mock_repo

        mock_tmpdir = "/tmp/test-repo"  # NOQA
        mock_tempdir.return_value.__enter__ = Mock(return_value=mock_tmpdir)
        mock_tempdir.return_value.__exit__ = Mock(return_value=None)

        repository = swe_client.get_repository("psf/requests")
        with swe_client.load_repo(repository, "main"):
            result = swe_client.repository_branch_exists("psf/requests", "main")

        assert result is True
        mock_repo.git.fetch.assert_called_once_with("origin", "main")

    @patch("codebase.clients.swe.Repo.clone_from")
    @patch("codebase.clients.swe.tempfile.TemporaryDirectory")
    def test_repository_branch_exists_false(self, mock_tempdir, mock_clone, swe_client):
        """Test branch existence check when branch doesn't exist."""
        mock_repo = Mock()
        mock_repo.git.fetch = Mock(side_effect=Exception("Branch not found"))
        mock_repo.git.checkout = Mock()
        mock_clone.return_value = mock_repo

        mock_tmpdir = "/tmp/test-repo"  # NOQA
        mock_tempdir.return_value.__enter__ = Mock(return_value=mock_tmpdir)
        mock_tempdir.return_value.__exit__ = Mock(return_value=None)

        repository = swe_client.get_repository("psf/requests")
        with swe_client.load_repo(repository, "main"):
            result = swe_client.repository_branch_exists("psf/requests", "nonexistent")

        assert result is False

    def test_current_user(self, swe_client):
        """Test current_user property."""
        user = swe_client.current_user

        assert user.id == 0
        assert user.username == "swe-bench"
        assert user.name == "SWE Bench"

    def test_unsupported_methods_raise_not_implemented(self, swe_client):
        """Test that unsupported methods raise NotImplementedError."""
        unsupported_methods = [
            ("set_repository_webhooks", ("repo", "url")),
            ("update_or_create_merge_request", ("repo", "source", "target", "title", "desc")),
            ("create_merge_request_comment", ("repo", 1, "body")),
            ("get_issue", ("repo", 1)),
            ("create_issue_comment", ("repo", 1, "body")),
            ("update_issue_comment", ("repo", 1, 1, "body")),
            ("create_issue_note_emoji", ("repo", 1, Mock(), "note_id")),
            ("get_issue_comment", ("repo", 1, "comment_id")),
            ("get_issue_related_merge_requests", ("repo", 1)),
            ("get_merge_request", ("repo", 1)),
            ("get_merge_request_latest_pipelines", ("repo", 1)),
            ("get_merge_request_review_comments", ("repo", 1)),
            ("get_merge_request_comments", ("repo", 1)),
            ("get_merge_request_comment", ("repo", 1, "comment_id")),
            ("create_merge_request_note_emoji", ("repo", 1, Mock(), "note_id")),
            ("mark_merge_request_comment_as_resolved", ("repo", 1, "discussion_id")),
            ("get_job", ("repo", 1)),
            ("job_log_trace", ("repo", 1)),
        ]

        for method_name, args in unsupported_methods:
            method = getattr(swe_client, method_name)
            with pytest.raises(NotImplementedError, match="does not support"):
                method(*args)
