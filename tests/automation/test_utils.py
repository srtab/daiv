from unittest.mock import Mock, patch

import pytest
from langgraph.store.memory import InMemoryStore

from automation.utils import (
    check_file_read,
    delete_file_change,
    file_changes_namespace,
    file_reads_namespace,
    get_file_change,
    get_file_changes,
    has_file_changes,
    register_file_change,
    register_file_read,
)
from codebase.base import FileChange, FileChangeAction
from codebase.context import RepositoryCtx


class TestNamespaceFunctions:
    """Test namespace generation functions"""

    def test_file_changes_namespace(self):
        """Test that file_changes_namespace returns the correct namespace tuple"""
        repo_id = "test_repo"
        ref = "main"

        namespace = file_changes_namespace(repo_id, ref)

        assert namespace == ("test_repo", "main", "file_changes")

    def test_file_reads_namespace(self):
        """Test that file_reads_namespace returns the correct namespace tuple"""
        repo_id = "test_repo"
        ref = "feature-branch"

        namespace = file_reads_namespace(repo_id, ref)

        assert namespace == ("test_repo", "feature-branch", "file_reads")


class TestRegisterFileChange:
    """Test register_file_change function with various scenarios"""

    @pytest.fixture
    def store(self):
        """Provide a clean InMemoryStore for each test"""
        return InMemoryStore()

    @pytest.fixture
    def mock_repository_ctx(self):
        """Mock repository context"""
        with patch("automation.utils.get_repository_ctx") as mock_get_ctx:
            mock_ctx = Mock(spec=RepositoryCtx)
            mock_ctx.repo_id = "test_repo"
            mock_ctx.ref = "main"
            mock_get_ctx.return_value = mock_ctx
            yield mock_ctx

    async def test_register_file_change_create_action(self, store, mock_repository_ctx):
        """Test registering a CREATE action creates correct FileChange"""
        old_content = ""
        old_path = ""
        new_content = "print('hello world')"
        new_path = "hello.py"

        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content=old_content,
            old_file_path=old_path,
            new_file_content=new_content,
            new_file_path=new_path,
        )

        # Verify the file change was stored
        file_change = await get_file_change(store, new_path)
        assert file_change is not None
        assert file_change.action == FileChangeAction.CREATE
        assert file_change.file_path == new_path
        assert file_change.content == new_content
        assert "a/dev/null" in file_change.diff_hunk
        assert f"b/{new_path}" in file_change.diff_hunk

    async def test_register_file_change_update_action(self, store, mock_repository_ctx):
        """Test registering an UPDATE action creates correct FileChange"""
        old_content = "print('hello')"
        new_content = "print('hello world')"
        file_path = "hello.py"

        await register_file_change(
            store=store,
            action=FileChangeAction.UPDATE,
            old_file_content=old_content,
            old_file_path=file_path,
            new_file_content=new_content,
            new_file_path=file_path,
        )

        file_change = await get_file_change(store, file_path)
        assert file_change is not None
        assert file_change.action == FileChangeAction.UPDATE
        assert file_change.file_path == file_path
        assert file_change.content == new_content
        assert f"a/{file_path}" in file_change.diff_hunk
        assert f"b/{file_path}" in file_change.diff_hunk

    async def test_register_file_change_delete_action(self, store, mock_repository_ctx):
        """Test registering a DELETE action creates correct FileChange"""
        old_content = "print('hello world')"
        file_path = "hello.py"

        await register_file_change(
            store=store,
            action=FileChangeAction.DELETE,
            old_file_content=old_content,
            old_file_path=file_path,
            new_file_content="",
        )

        file_change = await get_file_change(store, file_path)
        assert file_change is not None
        assert file_change.action == FileChangeAction.DELETE
        assert file_change.file_path == file_path
        assert file_change.content == old_content
        assert f"a/{file_path}" in file_change.diff_hunk
        assert "a/dev/null" in file_change.diff_hunk

    async def test_register_file_change_move_action(self, store, mock_repository_ctx):
        """Test registering a MOVE action creates correct FileChange"""
        old_content = "print('hello world')"
        old_path = "old_hello.py"
        new_path = "new_hello.py"

        await register_file_change(
            store=store,
            action=FileChangeAction.MOVE,
            old_file_content=old_content,
            old_file_path=old_path,
            new_file_content=old_content,
            new_file_path=new_path,
        )

        file_change = await get_file_change(store, new_path)
        assert file_change is not None
        assert file_change.action == FileChangeAction.MOVE
        assert file_change.file_path == new_path
        assert file_change.previous_path == old_path
        assert file_change.content == old_content

    async def test_register_file_change_defaults_to_old_values(self, store, mock_repository_ctx):
        """Test that new_file_content and new_file_path default to old values when not provided"""
        old_content = "print('hello')"
        old_path = "hello.py"

        await register_file_change(
            store=store, action=FileChangeAction.UPDATE, old_file_content=old_content, old_file_path=old_path
        )

        file_change = await get_file_change(store, old_path)
        assert file_change is not None
        assert file_change.content == old_content
        assert file_change.file_path == old_path

    async def test_register_file_change_delete_then_create_becomes_update(self, store, mock_repository_ctx):
        """Test that DELETE followed by CREATE becomes UPDATE"""
        file_path = "test.py"
        original_content = "original content"
        new_content = "new content"

        # First register DELETE
        await register_file_change(
            store=store, action=FileChangeAction.DELETE, old_file_content=original_content, old_file_path=file_path
        )

        # Then register CREATE on same path
        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content="",
            old_file_path=None,
            new_file_content=new_content,
            new_file_path=file_path,
        )

        file_change = await get_file_change(store, file_path)
        assert file_change is not None
        assert file_change.action == FileChangeAction.UPDATE
        assert file_change.content == new_content

    async def test_register_file_change_create_then_delete_removes_change(self, store, mock_repository_ctx):
        """Test that CREATE followed by DELETE removes the file change entirely"""
        file_path = "test.py"
        content = "test content"

        # First register CREATE
        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content="",
            old_file_path="",
            new_file_content=content,
            new_file_path=file_path,
        )

        # Verify it exists
        file_change = await get_file_change(store, file_path)
        assert file_change is not None

        # Then register DELETE
        await register_file_change(
            store=store, action=FileChangeAction.DELETE, old_file_content=content, old_file_path=file_path
        )

        # Verify it's removed
        file_change = await get_file_change(store, file_path)
        assert file_change is None

    async def test_register_file_change_create_then_move_stays_create(self, store, mock_repository_ctx):
        """Test that CREATE followed by MOVE maintains CREATE action"""
        old_path = "old.py"
        new_path = "new.py"
        content = "test content"

        # First register CREATE
        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content="",
            old_file_path=None,
            new_file_content=content,
            new_file_path=old_path,
        )

        # Then register MOVE
        await register_file_change(
            store=store,
            action=FileChangeAction.MOVE,
            old_file_content=content,
            old_file_path=old_path,
            new_file_content=content,
            new_file_path=new_path,
        )

        file_change = await get_file_change(store, new_path)
        assert file_change is not None
        assert file_change.action == FileChangeAction.CREATE
        assert file_change.file_path == new_path
        assert file_change.previous_path is None

    async def test_register_file_change_update_maintains_original_action(self, store, mock_repository_ctx):
        """Test that UPDATE on an existing change maintains the original action"""
        file_path = "test.py"
        intermediate_content = "intermediate"
        final_content = "final"

        # First register CREATE
        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content="",
            old_file_path="",
            new_file_content=intermediate_content,
            new_file_path=file_path,
        )

        # Then register UPDATE
        await register_file_change(
            store=store,
            action=FileChangeAction.UPDATE,
            old_file_content=intermediate_content,
            old_file_path=file_path,
            new_file_content=final_content,
            new_file_path=file_path,
        )

        file_change = await get_file_change(store, file_path)
        assert file_change is not None
        assert file_change.action == FileChangeAction.CREATE  # Should maintain CREATE
        assert file_change.content == final_content


class TestFileChangeQueries:
    """Test file change query functions"""

    @pytest.fixture
    def store(self):
        """Provide a clean InMemoryStore for each test"""
        return InMemoryStore()

    @pytest.fixture
    def mock_repository_ctx(self):
        """Mock repository context"""
        with patch("automation.utils.get_repository_ctx") as mock_get_ctx:
            mock_ctx = Mock(spec=RepositoryCtx)
            mock_ctx.repo_id = "test_repo"
            mock_ctx.ref = "main"
            mock_get_ctx.return_value = mock_ctx
            yield mock_ctx

    async def test_has_file_changes_empty_store(self, store, mock_repository_ctx):
        """Test has_file_changes returns False when no changes exist"""
        result = await has_file_changes(store)
        assert result is False

    async def test_has_file_changes_with_changes(self, store, mock_repository_ctx):
        """Test has_file_changes returns True when changes exist"""
        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content="",
            old_file_path="",
            new_file_content="test",
            new_file_path="test.py",
        )

        result = await has_file_changes(store)
        assert result is True

    async def test_get_file_changes_empty_store(self, store, mock_repository_ctx):
        """Test get_file_changes returns empty list when no changes exist"""
        changes = await get_file_changes(store)
        assert changes == []

    async def test_get_file_changes_with_multiple_changes(self, store, mock_repository_ctx):
        """Test get_file_changes returns all registered changes"""
        # Register multiple changes
        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content="",
            old_file_path="",
            new_file_content="content1",
            new_file_path="file1.py",
        )

        await register_file_change(
            store=store,
            action=FileChangeAction.UPDATE,
            old_file_content="old",
            old_file_path="file2.py",
            new_file_content="new",
            new_file_path="file2.py",
        )

        changes = await get_file_changes(store)
        assert len(changes) == 2
        assert all(isinstance(change, FileChange) for change in changes)

        # Check that both files are represented
        file_paths = {change.file_path for change in changes}
        assert file_paths == {"file1.py", "file2.py"}

    async def test_get_file_change_nonexistent_file(self, store, mock_repository_ctx):
        """Test get_file_change returns None for non-existent file"""
        result = await get_file_change(store, "nonexistent.py")
        assert result is None


class TestDeleteFileChange:
    """Test delete_file_change function"""

    @pytest.fixture
    def store(self):
        """Provide a clean InMemoryStore for each test"""
        return InMemoryStore()

    @pytest.fixture
    def mock_repository_ctx(self):
        """Mock repository context"""
        with patch("automation.utils.get_repository_ctx") as mock_get_ctx:
            mock_ctx = Mock(spec=RepositoryCtx)
            mock_ctx.repo_id = "test_repo"
            mock_ctx.ref = "main"
            mock_get_ctx.return_value = mock_ctx
            yield mock_ctx

    async def test_delete_file_change_existing_file(self, store, mock_repository_ctx):
        """Test deleting an existing file change"""
        file_path = "test.py"

        # First register a change
        await register_file_change(
            store=store,
            action=FileChangeAction.CREATE,
            old_file_content="",
            old_file_path="",
            new_file_content="test",
            new_file_path=file_path,
        )

        # Verify it exists
        file_change = await get_file_change(store, file_path)
        assert file_change is not None

        # Delete it
        await delete_file_change(store, file_path)

        # Verify it's gone
        file_change = await get_file_change(store, file_path)
        assert file_change is None

    async def test_delete_file_change_nonexistent_file(self, store, mock_repository_ctx):
        """Test deleting a non-existent file change doesn't raise error"""
        # Should not raise an exception
        await delete_file_change(store, "nonexistent.py")

        # Store should still be empty
        changes = await get_file_changes(store)
        assert changes == []


class TestFileReadFunctions:
    """Test file read tracking functions"""

    @pytest.fixture
    def store(self):
        """Provide a clean InMemoryStore for each test"""
        return InMemoryStore()

    @pytest.fixture
    def mock_repository_ctx(self):
        """Mock repository context"""
        with patch("automation.utils.get_repository_ctx") as mock_get_ctx:
            mock_ctx = Mock(spec=RepositoryCtx)
            mock_ctx.repo_id = "test_repo"
            mock_ctx.ref = "main"
            mock_get_ctx.return_value = mock_ctx
            yield mock_ctx

    async def test_register_file_read(self, store, mock_repository_ctx):
        """Test registering a file read"""
        file_path = "test.py"

        await register_file_read(store, file_path)

        # Verify it was registered
        result = await check_file_read(store, file_path)
        assert result is True

    async def test_check_file_read_unread_file(self, store, mock_repository_ctx):
        """Test checking a file that hasn't been read"""
        result = await check_file_read(store, "unread.py")
        assert result is False

    async def test_register_multiple_file_reads(self, store, mock_repository_ctx):
        """Test registering multiple file reads"""
        files = ["file1.py", "file2.py", "file3.py"]

        for file_path in files:
            await register_file_read(store, file_path)

        # Check all files are marked as read
        for file_path in files:
            result = await check_file_read(store, file_path)
            assert result is True

        # Check a file that wasn't read
        result = await check_file_read(store, "unread.py")
        assert result is False
