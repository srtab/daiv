from unittest.mock import Mock, patch

import pytest
from langgraph.store.memory import InMemoryStore

from automation.utils import check_file_read, file_reads_namespace, register_file_read
from codebase.context import RuntimeCtx


class TestNamespaceFunctions:
    """Test namespace generation functions"""

    def test_file_reads_namespace(self):
        """Test that file_reads_namespace returns the correct namespace tuple"""
        repo_id = "test_repo"
        ref = "feature-branch"

        namespace = file_reads_namespace(repo_id, ref)

        assert namespace == ("test_repo", "feature-branch", "file_reads")


class TestFileReadFunctions:
    """Test file read tracking functions"""

    @pytest.fixture
    def store(self):
        """Provide a clean InMemoryStore for each test"""
        return InMemoryStore()

    @pytest.fixture
    def mock_runtime_ctx(self):
        """Mock runtime context"""
        with patch("automation.utils.get_runtime_ctx") as mock_get_ctx:
            mock_ctx = Mock(spec=RuntimeCtx)
            mock_ctx.repo_id = "test_repo"
            mock_ctx.repo = Mock(active_branch=Mock())
            mock_ctx.repo.active_branch.configure_mock(name="main")
            mock_get_ctx.return_value = mock_ctx
            yield mock_ctx

    async def test_register_file_read(self, store, mock_runtime_ctx):
        """Test registering a file read"""
        file_path = "test.py"

        await register_file_read(store, file_path)

        # Verify it was registered
        result = await check_file_read(store, file_path)
        assert result is True

    async def test_check_file_read_unread_file(self, store, mock_runtime_ctx):
        """Test checking a file that hasn't been read"""
        result = await check_file_read(store, "unread.py")
        assert result is False

    async def test_register_multiple_file_reads(self, store, mock_runtime_ctx):
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
