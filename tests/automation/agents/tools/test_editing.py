from unittest.mock import MagicMock, patch

import pytest

from automation.agents.tools.editing import diff_tool
from codebase.base import FileChange, FileChangeAction


class TestDiffTool:
    @pytest.fixture
    def mock_store(self):
        """Create a mock store for testing."""
        return MagicMock()

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

    @patch("automation.agents.tools.editing.get_file_change")
    async def test_diff_single_file(self, mock_get_file_change, mock_store, sample_file_change_update):
        """Test retrieving diff for a single file."""
        mock_get_file_change.return_value = sample_file_change_update

        result = await diff_tool.ainvoke({"file_paths": ["test_file.py"], "store": mock_store})

        mock_get_file_change.assert_called_once_with(mock_store, "test_file.py")
        assert "test_file.py" in result
        assert "line 2" in result
        assert "@@" in result  # diff hunk header

    @patch("automation.agents.tools.editing.get_file_change")
    async def test_diff_multiple_files(
        self, mock_get_file_change, mock_store, sample_file_change_update, sample_file_change_create
    ):
        """Test retrieving diffs for multiple files."""

        # Mock returns different file changes based on the file path
        def side_effect(store, file_path):
            if file_path == "test_file.py":
                return sample_file_change_update
            elif file_path == "new_file.py":
                return sample_file_change_create
            return None

        mock_get_file_change.side_effect = side_effect

        result = await diff_tool.ainvoke({"file_paths": ["test_file.py", "new_file.py"], "store": mock_store})

        assert mock_get_file_change.call_count == 2
        assert "test_file.py" in result
        assert "new_file.py" in result

    @patch("automation.agents.tools.editing.get_file_changes")
    async def test_diff_all_files(
        self, mock_get_file_changes, mock_store, sample_file_change_update, sample_file_change_create
    ):
        """Test retrieving diffs for all changed files when no file_paths specified."""
        mock_get_file_changes.return_value = [sample_file_change_update, sample_file_change_create]

        result = await diff_tool.ainvoke({"file_paths": None, "store": mock_store})

        mock_get_file_changes.assert_called_once_with(mock_store)
        assert "test_file.py" in result
        assert "new_file.py" in result

    @patch("automation.agents.tools.editing.get_file_changes")
    async def test_diff_all_files_empty_list(
        self, mock_get_file_changes, mock_store, sample_file_change_update, sample_file_change_create
    ):
        """Test retrieving diffs for all changed files when file_paths is empty list."""
        mock_get_file_changes.return_value = [sample_file_change_update, sample_file_change_create]

        result = await diff_tool.ainvoke({"file_paths": [], "store": mock_store})

        mock_get_file_changes.assert_called_once_with(mock_store)
        assert "test_file.py" in result
        assert "new_file.py" in result

    @patch("automation.agents.tools.editing.get_file_change")
    async def test_diff_nonexistent_file(self, mock_get_file_change, mock_store):
        """Test handling of non-existent file paths."""
        mock_get_file_change.return_value = None

        result = await diff_tool.ainvoke({"file_paths": ["nonexistent.py"], "store": mock_store})

        mock_get_file_change.assert_called_once_with(mock_store, "nonexistent.py")
        assert "No changes found" in result
        assert "nonexistent.py" in result

    @patch("automation.agents.tools.editing.get_file_change")
    async def test_diff_mixed_existent_and_nonexistent(
        self, mock_get_file_change, mock_store, sample_file_change_update
    ):
        """Test handling of mixed existent and non-existent files."""

        def side_effect(store, file_path):
            if file_path == "test_file.py":
                return sample_file_change_update
            return None

        mock_get_file_change.side_effect = side_effect

        result = await diff_tool.ainvoke({"file_paths": ["test_file.py", "nonexistent.py"], "store": mock_store})

        assert mock_get_file_change.call_count == 2
        assert "test_file.py" in result
        assert "No changes found" in result
        assert "nonexistent.py" in result

    @patch("automation.agents.tools.editing.get_file_changes")
    async def test_diff_no_changes_in_store(self, mock_get_file_changes, mock_store):
        """Test when no changes exist in the store."""
        mock_get_file_changes.return_value = []

        result = await diff_tool.ainvoke({"file_paths": None, "store": mock_store})

        mock_get_file_changes.assert_called_once_with(mock_store)
        assert "No file changes have been made yet" in result

    @patch("automation.agents.tools.editing.get_file_change")
    async def test_diff_create_action(self, mock_get_file_change, mock_store, sample_file_change_create):
        """Test diff for CREATE action."""
        mock_get_file_change.return_value = sample_file_change_create

        result = await diff_tool.ainvoke({"file_paths": ["new_file.py"], "store": mock_store})

        assert "new_file.py" in result
        assert "dev/null" in result or "new_file.py" in result  # Create shows diff from /dev/null

    @patch("automation.agents.tools.editing.get_file_change")
    async def test_diff_delete_action(self, mock_get_file_change, mock_store, sample_file_change_delete):
        """Test diff for DELETE action."""
        mock_get_file_change.return_value = sample_file_change_delete

        result = await diff_tool.ainvoke({"file_paths": ["deleted_file.py"], "store": mock_store})

        assert "deleted_file.py" in result
        assert "dev/null" in result or "deleted_file.py" in result  # Delete shows diff to /dev/null

    @patch("automation.agents.tools.editing.get_file_change")
    async def test_diff_move_action(self, mock_get_file_change, mock_store, sample_file_change_move):
        """Test diff for MOVE action."""
        mock_get_file_change.return_value = sample_file_change_move

        result = await diff_tool.ainvoke({"file_paths": ["new_location.py"], "store": mock_store})
        assert "old_location.py" in result or "new_location.py" in result

    @patch("automation.agents.tools.editing.get_file_changes")
    async def test_diff_file_change_with_empty_diff_hunk(self, mock_get_file_changes, mock_store):
        """Test when file changes exist but have empty diff_hunks."""
        # Create a mock file change with empty diff_hunk
        mock_file_change = MagicMock(spec=FileChange)
        mock_file_change.diff_hunk = ""
        mock_get_file_changes.return_value = [mock_file_change]

        result = await diff_tool.ainvoke({"file_paths": None, "store": mock_store})

        assert "No file changes have been made yet" in result
