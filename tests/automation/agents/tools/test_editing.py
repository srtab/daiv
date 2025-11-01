from unittest.mock import MagicMock

import pytest
from langchain.tools import ToolRuntime

from codebase.base import FileChange, FileChangeAction


class TestDiffTool:
    @pytest.fixture
    def mock_tool_runtime(self):
        """Create a mock store for testing."""
        return ToolRuntime(
            state={}, context=None, config=None, stream_writer=None, tool_call_id=None, store=MagicMock()
        )

    @pytest.fixture
    def sample_file_change_update(self):
        """Create a sample file change for testing."""
        return FileChange(
            action=FileChangeAction.UPDATE,
            file_path="test_file.py",
            original_content="line 1\nline 2\nline 3\n",
            content="line 1\nline 2 modified\nline 3\n",
        )

    @pytest.fixture
    def sample_file_change_create(self):
        """Create a sample file change for file creation."""
        return FileChange(
            action=FileChangeAction.CREATE,
            file_path="new_file.py",
            original_content="",
            content="new line 1\nnew line 2\n",
        )

    @pytest.fixture
    def sample_file_change_delete(self):
        """Create a sample file change for file deletion."""
        return FileChange(
            action=FileChangeAction.DELETE,
            file_path="deleted_file.py",
            original_content="old line 1\nold line 2\n",
            content="",
        )

    @pytest.fixture
    def sample_file_change_move(self):
        """Create a sample file change for file move/rename."""
        return FileChange(
            action=FileChangeAction.MOVE,
            file_path="new_location.py",
            previous_path="old_location.py",
            original_content="line 1\nline 2\n",
            content="line 1\nline 2\n",
        )
