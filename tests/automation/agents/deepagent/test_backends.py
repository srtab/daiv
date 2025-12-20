"""Tests for FilesystemBackend and DAIVStateBackend delete/rename operations."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation.agents.deepagent.backends import FilesystemBackend, StateBackend


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


class TestDAIVStateBackend:
    """Tests for DAIVStateBackend delete and rename operations."""

    @pytest.fixture
    def runtime(self):
        """Create a mock runtime with state."""
        runtime = MagicMock()
        runtime.state = {
            "files": {
                "/file1.txt": {
                    "content": ["line1", "line2"],
                    "created_at": "2024-01-01T00:00:00",
                    "modified_at": "2024-01-01T00:00:00",
                },
                "/file2.txt": {
                    "content": ["content"],
                    "created_at": "2024-01-01T00:00:00",
                    "modified_at": "2024-01-01T00:00:00",
                },
                "/dir/file3.txt": {
                    "content": ["nested"],
                    "created_at": "2024-01-01T00:00:00",
                    "modified_at": "2024-01-01T00:00:00",
                },
                "/dir/subdir/file4.txt": {
                    "content": ["deep"],
                    "created_at": "2024-01-01T00:00:00",
                    "modified_at": "2024-01-01T00:00:00",
                },
            }
        }
        return runtime

    @pytest.fixture
    def backend(self, runtime):
        """Create a DAIVStateBackend."""
        return StateBackend(runtime)

    def test_delete_file_success(self, backend):
        """Test deleting a file from state."""
        result = backend.delete("/file1.txt")

        assert result.error is None
        assert result.path == "/file1.txt"
        assert result.files_update == {"/file1.txt": None}

    def test_delete_file_not_found(self, backend):
        """Test deleting a non-existent file."""
        result = backend.delete("/nonexistent.txt")

        assert result.error == "Error: Path '/nonexistent.txt' does not exist"
        assert result.path is None

    def test_delete_directory_without_recursive(self, backend):
        """Test that deleting a directory without recursive=True fails."""
        result = backend.delete("/dir")

        assert "is a directory" in result.error
        assert "recursive=True" in result.error

    def test_delete_directory_with_recursive(self, backend):
        """Test deleting a directory with recursive=True."""
        result = backend.delete("/dir", recursive=True)

        assert result.error is None
        assert result.path == "/dir"
        # Should delete all files under /dir/
        assert result.files_update == {"/dir/file3.txt": None, "/dir/subdir/file4.txt": None}

    def test_rename_file_success(self, backend):
        """Test renaming a file in state."""
        result = backend.rename("/file1.txt", "/renamed.txt")

        assert result.error is None
        assert result.old_path == "/file1.txt"
        assert result.new_path == "/renamed.txt"
        # Should delete old and create new
        assert result.files_update["/file1.txt"] is None
        assert result.files_update["/renamed.txt"]["content"] == ["line1", "line2"]

    def test_rename_file_not_found(self, backend):
        """Test renaming a non-existent file."""
        result = backend.rename("/nonexistent.txt", "/new.txt")

        assert result.error == "Error: Path '/nonexistent.txt' does not exist"
        assert result.old_path is None
        assert result.new_path is None

    def test_rename_file_target_exists(self, backend):
        """Test that renaming fails when target already exists."""
        result = backend.rename("/file1.txt", "/file2.txt")

        assert result.error == "Error: Path '/file2.txt' already exists"

    def test_rename_directory(self, backend):
        """Test renaming a directory in state."""
        result = backend.rename("/dir", "/newdir")

        assert result.error is None
        assert result.old_path == "/dir"
        assert result.new_path == "/newdir"

        # Should delete all old paths and create new ones
        files_update = result.files_update
        assert files_update["/dir/file3.txt"] is None
        assert files_update["/dir/subdir/file4.txt"] is None
        assert files_update["/newdir/file3.txt"]["content"] == ["nested"]
        assert files_update["/newdir/subdir/file4.txt"]["content"] == ["deep"]

    def test_rename_directory_target_exists_as_file(self, backend):
        """Test that renaming directory fails when target exists as file."""
        result = backend.rename("/dir", "/file1.txt")

        assert result.error == "Error: Path '/file1.txt' already exists"

    def test_rename_directory_target_exists_as_directory(self, backend, runtime):
        """Test that renaming directory fails when target directory has files."""
        # Add a file that would conflict with the target
        runtime.state["files"]["/other/test.txt"] = {
            "content": ["test"],
            "created_at": "2024-01-01T00:00:00",
            "modified_at": "2024-01-01T00:00:00",
        }

        result = backend.rename("/dir", "/other")

        assert result.error == "Error: Path '/other' already exists"

    async def test_async_delete(self, backend):
        """Test async delete operation."""
        result = await backend.adelete("/file1.txt")

        assert result.error is None
        assert result.files_update == {"/file1.txt": None}

    async def test_async_rename(self, backend):
        """Test async rename operation."""
        result = await backend.arename("/file1.txt", "/renamed.txt")

        assert result.error is None
        assert result.files_update["/file1.txt"] is None
        assert result.files_update["/renamed.txt"] is not None
