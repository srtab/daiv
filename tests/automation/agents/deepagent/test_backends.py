"""Tests for FilesystemBackend and DAIVStateBackend delete/rename operations."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation.agents.deepagent.backends import CompositeBackend, FilesystemBackend, StateBackend


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


class TestCompositeBackend:
    """Tests for CompositeBackend delete and rename operations with routing."""

    @pytest.fixture
    def temp_root(self):
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def state_runtime(self):
        """Create a mock runtime with state for StateBackend."""
        runtime = MagicMock()
        runtime.state = {
            "files": {
                "/skill1.txt": {
                    "content": ["skill content"],
                    "created_at": "2024-01-01T00:00:00",
                    "modified_at": "2024-01-01T00:00:00",
                },
                "/skill2.txt": {
                    "content": ["another skill"],
                    "created_at": "2024-01-01T00:00:00",
                    "modified_at": "2024-01-01T00:00:00",
                },
            }
        }
        return runtime

    @pytest.fixture
    def default_runtime(self):
        """Create a mock runtime for the default backend."""
        runtime = MagicMock()
        runtime.state = {"files": {}}
        return runtime

    @pytest.fixture
    def composite_backend(self, temp_root, state_runtime, default_runtime):
        """Create a CompositeBackend with FilesystemBackend as default and StateBackend for /skills/."""
        filesystem_backend = FilesystemBackend(root_dir=temp_root, virtual_mode=True)
        filesystem_backend.runtime = default_runtime  # Add runtime for state merging
        state_backend = StateBackend(state_runtime)

        return CompositeBackend(default=filesystem_backend, routes={"/skills/": state_backend})

    def test_delete_file_from_default_backend(self, composite_backend, temp_root):
        """Test deleting a file from the default FilesystemBackend."""
        # Create a test file
        test_file = temp_root / "test.txt"
        test_file.write_text("test content")

        # Delete via composite backend
        result = composite_backend.delete("/test.txt")

        assert result.error is None
        assert result.path == "/test.txt"
        assert result.files_update is None  # FilesystemBackend doesn't use files_update
        assert not test_file.exists()

    def test_delete_file_from_routed_backend(self, composite_backend, state_runtime):
        """Test deleting a file from the routed StateBackend."""
        # Delete via composite backend (routes to StateBackend)
        result = composite_backend.delete("/skills/skill1.txt")

        assert result.error is None
        assert result.path == "/skills/skill1.txt"
        assert result.files_update == {"/skill1.txt": None}  # StateBackend returns files_update

    def test_delete_directory_recursive_from_default(self, composite_backend, temp_root):
        """Test deleting a directory recursively from default backend."""
        # Create a test directory with files
        test_dir = temp_root / "testdir"
        test_dir.mkdir()
        (test_dir / "file1.txt").write_text("content1")
        (test_dir / "file2.txt").write_text("content2")

        # Delete via composite backend
        result = composite_backend.delete("/testdir", recursive=True)

        assert result.error is None
        assert result.path == "/testdir"
        assert not test_dir.exists()

    def test_delete_nonexistent_file(self, composite_backend):
        """Test deleting a non-existent file."""
        result = composite_backend.delete("/nonexistent.txt")

        assert result.error == "Error: Path '/nonexistent.txt' does not exist"
        assert result.path is None

    def test_rename_file_in_default_backend(self, composite_backend, temp_root):
        """Test renaming a file in the default FilesystemBackend."""
        # Create a test file
        test_file = temp_root / "old.txt"
        test_file.write_text("test content")

        # Rename via composite backend
        result = composite_backend.rename("/old.txt", "/new.txt")

        assert result.error is None
        assert result.old_path == "/old.txt"
        assert result.new_path == "/new.txt"
        assert not test_file.exists()
        assert (temp_root / "new.txt").exists()
        assert (temp_root / "new.txt").read_text() == "test content"

    def test_rename_file_in_routed_backend(self, composite_backend, state_runtime):
        """Test renaming a file in the routed StateBackend."""
        # Rename via composite backend (routes to StateBackend)
        result = composite_backend.rename("/skills/skill1.txt", "/skills/renamed.txt")

        assert result.error is None
        assert result.old_path == "/skills/skill1.txt"
        assert result.new_path == "/skills/renamed.txt"
        # Check files_update from StateBackend
        assert result.files_update["/skill1.txt"] is None
        assert result.files_update["/renamed.txt"] is not None

    def test_rename_across_backends_fails(self, composite_backend, temp_root):
        """Test that renaming across different backends fails."""
        # Create a file in the default backend
        test_file = temp_root / "test.txt"
        test_file.write_text("test content")

        # Try to rename from default backend to routed backend
        result = composite_backend.rename("/test.txt", "/skills/test.txt")

        assert result.error is not None
        assert "Cannot rename across different backends" in result.error
        assert test_file.exists()  # Original file should still exist

    def test_rename_from_routed_to_default_fails(self, composite_backend):
        """Test that renaming from routed backend to default fails."""
        # Try to rename from routed backend to default backend
        result = composite_backend.rename("/skills/skill1.txt", "/regular.txt")

        assert result.error is not None
        assert "Cannot rename across different backends" in result.error

    def test_rename_target_exists(self, composite_backend, temp_root):
        """Test that renaming fails when target already exists."""
        # Create both files
        old_file = temp_root / "old.txt"
        old_file.write_text("old content")
        new_file = temp_root / "new.txt"
        new_file.write_text("new content")

        # Try to rename
        result = composite_backend.rename("/old.txt", "/new.txt")

        assert result.error == "Error: Path '/new.txt' already exists"
        assert old_file.exists()
        assert new_file.read_text() == "new content"

    async def test_async_delete_from_default(self, composite_backend, temp_root):
        """Test async delete from default backend."""
        test_file = temp_root / "async_test.txt"
        test_file.write_text("content")

        result = await composite_backend.adelete("/async_test.txt")

        assert result.error is None
        assert not test_file.exists()

    async def test_async_delete_from_routed(self, composite_backend):
        """Test async delete from routed backend."""
        result = await composite_backend.adelete("/skills/skill1.txt")

        assert result.error is None
        assert result.files_update == {"/skill1.txt": None}

    async def test_async_rename_from_default(self, composite_backend, temp_root):
        """Test async rename from default backend."""
        test_file = temp_root / "async_old.txt"
        test_file.write_text("content")

        result = await composite_backend.arename("/async_old.txt", "/async_new.txt")

        assert result.error is None
        assert not test_file.exists()
        assert (temp_root / "async_new.txt").exists()

    async def test_async_rename_from_routed(self, composite_backend):
        """Test async rename from routed backend."""
        result = await composite_backend.arename("/skills/skill1.txt", "/skills/renamed.txt")

        assert result.error is None
        assert result.files_update["/skill1.txt"] is None
        assert result.files_update["/renamed.txt"] is not None

    async def test_async_rename_across_backends_fails(self, composite_backend, temp_root):
        """Test that async rename across backends fails."""
        test_file = temp_root / "test.txt"
        test_file.write_text("test content")

        result = await composite_backend.arename("/test.txt", "/skills/test.txt")

        assert result.error is not None
        assert "Cannot rename across different backends" in result.error
