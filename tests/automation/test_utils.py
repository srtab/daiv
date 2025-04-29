from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from automation.tools.repository import RETRIEVE_FILE_CONTENT_NAME
from automation.utils import file_changes_namespace, prepare_repository_files_as_messages


class TestFileChangesNamespace:
    def test_basic_valid_inputs(self):
        result = file_changes_namespace("repo123", "main")
        assert isinstance(result, tuple)
        assert len(result) == 3
        assert result == ("file_changes", "repo123", "main")

    def test_empty_strings(self):
        result = file_changes_namespace("", "")
        assert isinstance(result, tuple)
        assert len(result) == 3
        assert result == ("file_changes", "", "")

    def test_special_characters(self):
        result = file_changes_namespace("repo/with-special_chars", "feature/branch-name")
        assert isinstance(result, tuple)
        assert len(result) == 3
        assert result == ("file_changes", "repo/with-special_chars", "feature/branch-name")

    def test_return_structure(self):
        result = file_changes_namespace("repo123", "main")
        assert result[0] == "file_changes"
        assert result[1] == "repo123"
        assert result[2] == "main"


class TestPrepareRepositoryFilesAsMessages:
    @pytest.fixture
    def mock_store(self):
        return MagicMock()

    def test_empty_paths_list(self, mock_store):
        result = prepare_repository_files_as_messages([], "repo123", "main", mock_store)
        assert isinstance(result, list)
        assert len(result) == 0

    @patch("automation.utils.RetrieveFileContentTool")
    def test_valid_paths_with_content(self, mock_tool_class, mock_store):
        # Setup mock tool
        mock_tool = MagicMock()
        mock_tool.invoke.return_value = "Sample file content"
        mock_tool_class.return_value = mock_tool

        # Test function
        paths = ["file1.py", "file2.py"]
        result = prepare_repository_files_as_messages(paths, "repo123", "main", mock_store)

        # Verify tool was called correctly
        mock_tool.invoke.assert_called_once_with(
            {
                "file_paths": paths,
                "intent": "[Manual call] Check current implementation of the files",
                "store": mock_store,
            },
            config={"configurable": {"source_repo_id": "repo123", "source_ref": "main"}},
        )

        # Verify result structure
        assert len(result) == 2
        assert isinstance(result[0], AIMessage)
        assert isinstance(result[1], ToolMessage)
        assert result[1].content == "Sample file content"

    @patch("automation.utils.RetrieveFileContentTool")
    def test_valid_paths_empty_content(self, mock_tool_class, mock_store):
        # Setup mock tool
        mock_tool = MagicMock()
        mock_tool.invoke.return_value = ""
        mock_tool_class.return_value = mock_tool

        # Test function
        paths = ["file1.py", "file2.py"]
        result = prepare_repository_files_as_messages(paths, "repo123", "main", mock_store)

        # Verify result is empty
        assert len(result) == 0

    @patch("automation.utils.RetrieveFileContentTool")
    def test_multiple_paths(self, mock_tool_class, mock_store):
        # Setup mock tool
        mock_tool = MagicMock()
        mock_tool.invoke.return_value = "Multiple file contents"
        mock_tool_class.return_value = mock_tool

        # Test function
        paths = ["file1.py", "file2.py", "file3.py"]
        result = prepare_repository_files_as_messages(paths, "repo123", "main", mock_store)

        # Verify tool was called with all paths
        mock_tool.invoke.assert_called_once()
        call_args = mock_tool.invoke.call_args[0][0]
        assert call_args["file_paths"] == paths
        assert len(paths) == 3

        # Verify result structure
        assert len(result) == 2

    @patch("automation.utils.RetrieveFileContentTool")
    def test_message_structure_and_content(self, mock_tool_class, mock_store):
        # Setup mock tool
        mock_tool = MagicMock()
        mock_tool.invoke.return_value = "File content"
        mock_tool_class.return_value = mock_tool

        # Test function
        paths = ["file1.py"]
        result = prepare_repository_files_as_messages(paths, "repo123", "main", mock_store)

        # Verify AIMessage structure
        ai_message = result[0]
        assert isinstance(ai_message, AIMessage)
        assert "I'll help you apply the code changes" in ai_message.content
        assert len(ai_message.tool_calls) == 1
        assert ai_message.tool_calls[0].name == RETRIEVE_FILE_CONTENT_NAME
        assert ai_message.tool_calls[0].args["file_paths"] == paths
        assert ai_message.tool_calls[0].args["intent"] == "[Manual call] Check current implementation of the files"

        # Verify ToolMessage structure
        tool_message = result[1]
        assert isinstance(tool_message, ToolMessage)
        assert tool_message.content == "File content"
        assert tool_message.tool_call_id == ai_message.tool_calls[0].id

    @patch("automation.utils.RetrieveFileContentTool")
    @patch("automation.utils.uuid.uuid4")
    def test_uuid_generation(self, mock_uuid4, mock_tool_class, mock_store):
        # Setup mocks
        mock_uuid = MagicMock()
        mock_uuid.return_value = "12345678-1234-5678-1234-567812345678"
        mock_uuid4.return_value = mock_uuid

        mock_tool = MagicMock()
        mock_tool.invoke.return_value = "File content"
        mock_tool_class.return_value = mock_tool

        # Test function
        paths = ["file1.py"]
        result = prepare_repository_files_as_messages(paths, "repo123", "main", mock_store)

        # Verify UUID was used correctly
        expected_tool_call_id = "call_123456781234567812345678567812345678"
        assert result[0].tool_calls[0].id == expected_tool_call_id
        assert result[1].tool_call_id == expected_tool_call_id

    @patch("automation.utils.RetrieveFileContentTool")
    def test_tool_call_id_passing(self, mock_tool_class, mock_store):
        # Setup mock tool
        mock_tool = MagicMock()
        mock_tool.invoke.return_value = "File content"
        mock_tool_class.return_value = mock_tool

        # Test function
        paths = ["file1.py"]
        result = prepare_repository_files_as_messages(paths, "repo123", "main", mock_store)

        # Verify tool_call_id is passed correctly between messages
        ai_message = result[0]
        tool_message = result[1]
        assert tool_message.tool_call_id == ai_message.tool_calls[0].id
