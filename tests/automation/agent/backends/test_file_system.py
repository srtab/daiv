from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from automation.agent.backends import FilesystemBackend


class TestFilesystemBackend:
    """Tests for FilesystemBackend delete and rename operations."""

    @pytest.fixture
    def temp_root(self):
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def backend(self, temp_root):
        """Create a FilesystemBackend with virtual mode."""
        return FilesystemBackend(root_dir=temp_root, virtual_mode=True)

    def test_delete_file_success(self, backend, temp_root):
        """Test deleting a file successfully."""
        # Create a test file
        test_file = temp_root / "test.txt"
        test_file.write_text("test content")

        # Delete the file
        result = backend.delete("/test.txt")

        assert result.error is None
        assert result.path == "/test.txt"
        assert result.files_update is None  # External storage
        assert not test_file.exists()

    def test_delete_file_not_found(self, backend):
        """Test deleting a non-existent file."""
        result = backend.delete("/nonexistent.txt")

        assert result.error == "Error: Path '/nonexistent.txt' does not exist"
        assert result.path is None

    def test_delete_directory_without_recursive(self, backend, temp_root):
        """Test that deleting a directory without recursive=True fails."""
        # Create a test directory
        test_dir = temp_root / "testdir"
        test_dir.mkdir()

        # Try to delete without recursive
        result = backend.delete("/testdir")

        assert "is a directory" in result.error
        assert "recursive=True" in result.error
        assert test_dir.exists()

    def test_delete_directory_with_recursive(self, backend, temp_root):
        """Test deleting a directory with recursive=True."""
        # Create a test directory with files
        test_dir = temp_root / "testdir"
        test_dir.mkdir()
        (test_dir / "file1.txt").write_text("content1")
        (test_dir / "subdir").mkdir()
        (test_dir / "subdir" / "file2.txt").write_text("content2")

        # Delete with recursive
        result = backend.delete("/testdir", recursive=True)

        assert result.error is None
        assert result.path == "/testdir"
        assert result.files_update is None
        assert not test_dir.exists()

    def test_delete_path_traversal_blocked(self, backend):
        """Test that path traversal is blocked."""
        result = backend.delete("/../etc/passwd")

        assert "Path traversal not allowed" in result.error

    def test_rename_file_success(self, backend, temp_root):
        """Test renaming a file successfully."""
        # Create a test file
        test_file = temp_root / "old.txt"
        test_file.write_text("test content")

        # Rename the file
        result = backend.rename("/old.txt", "/new.txt")

        assert result.error is None
        assert result.old_path == "/old.txt"
        assert result.new_path == "/new.txt"
        assert result.files_update is None
        assert not test_file.exists()
        assert (temp_root / "new.txt").exists()
        assert (temp_root / "new.txt").read_text() == "test content"

    def test_rename_file_to_subdirectory(self, backend, temp_root):
        """Test renaming a file to a subdirectory (creates parent dirs)."""
        # Create a test file
        test_file = temp_root / "old.txt"
        test_file.write_text("test content")

        # Rename to a subdirectory that doesn't exist yet
        result = backend.rename("/old.txt", "/subdir/new.txt")

        assert result.error is None
        assert result.old_path == "/old.txt"
        assert result.new_path == "/subdir/new.txt"
        assert not test_file.exists()
        assert (temp_root / "subdir" / "new.txt").exists()
        assert (temp_root / "subdir" / "new.txt").read_text() == "test content"

    def test_rename_file_not_found(self, backend):
        """Test renaming a non-existent file."""
        result = backend.rename("/nonexistent.txt", "/new.txt")

        assert result.error == "Error: Path '/nonexistent.txt' does not exist"
        assert result.old_path is None
        assert result.new_path is None

    def test_rename_file_target_exists(self, backend, temp_root):
        """Test that renaming fails when target already exists."""
        # Create both files
        old_file = temp_root / "old.txt"
        old_file.write_text("old content")
        new_file = temp_root / "new.txt"
        new_file.write_text("new content")

        # Try to rename
        result = backend.rename("/old.txt", "/new.txt")

        assert result.error == "Error: Path '/new.txt' already exists"
        assert old_file.exists()  # Old file still exists
        assert new_file.read_text() == "new content"  # New file unchanged

    def test_rename_directory(self, backend, temp_root):
        """Test renaming a directory."""
        # Create a test directory with files
        old_dir = temp_root / "olddir"
        old_dir.mkdir()
        (old_dir / "file.txt").write_text("content")

        # Rename the directory
        result = backend.rename("/olddir", "/newdir")

        assert result.error is None
        assert result.old_path == "/olddir"
        assert result.new_path == "/newdir"
        assert not old_dir.exists()
        assert (temp_root / "newdir").exists()
        assert (temp_root / "newdir" / "file.txt").read_text() == "content"

    def test_rename_path_traversal_blocked(self, backend, temp_root):
        """Test that path traversal is blocked in rename."""
        test_file = temp_root / "test.txt"
        test_file.write_text("content")

        result = backend.rename("/test.txt", "/../etc/passwd")

        assert "Path traversal not allowed" in result.error

    async def test_async_delete(self, backend, temp_root):
        """Test async delete operation."""
        test_file = temp_root / "async_test.txt"
        test_file.write_text("content")

        result = await backend.adelete("/async_test.txt")

        assert result.error is None
        assert not test_file.exists()

    async def test_async_rename(self, backend, temp_root):
        """Test async rename operation."""
        test_file = temp_root / "async_old.txt"
        test_file.write_text("content")

        result = await backend.arename("/async_old.txt", "/async_new.txt")

        assert result.error is None
        assert not test_file.exists()
        assert (temp_root / "async_new.txt").exists()
