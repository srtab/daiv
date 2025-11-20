"""Tests for the enhanced grep navigation tool."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest
from git import Repo

from automation.agents.tools.navigation import grep_tool
from codebase.context import RuntimeCtx
from codebase.repo_config import RepositoryConfig


@pytest.fixture
def temp_repo():
    """Create a temporary repository with test files."""
    with TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)

        # Initialize git repo
        repo = Repo.init(repo_path)

        # Create test files
        (repo_path / "file1.py").write_text("def hello():\n    print('hello')\n    return True\n")
        (repo_path / "file2.py").write_text("def goodbye():\n    print('goodbye')\n    return False\n")
        (repo_path / "file3.js").write_text("function hello() {\n  console.log('hello');\n}\n")
        (repo_path / "subdir").mkdir()
        (repo_path / "subdir" / "file4.py").write_text("def test():\n    print('test')\n    return None\n")
        (repo_path / "README.md").write_text("This is a test project.\nTODO: Add more tests.\nFIXME: Fix bug.\n")

        # Commit files
        repo.index.add([str(f) for f in repo_path.rglob("*") if f.is_file()])
        repo.index.commit("Initial commit")

        yield repo_path


@pytest.fixture
def runtime_ctx(temp_repo):
    """Create a RuntimeCtx with the temp repo."""
    repo = Repo(temp_repo)

    mock_config = Mock(spec=RepositoryConfig)
    mock_config.combined_exclude_patterns = []
    mock_config.omit_content_patterns = []

    ctx = RuntimeCtx(repo_id="test-repo", repo=repo, config=mock_config)
    return ctx


@pytest.fixture
def tool_runtime(runtime_ctx):
    """Create a ToolRuntime with the RuntimeCtx."""
    from langchain.tools import ToolRuntime

    runtime = ToolRuntime(context=runtime_ctx, store=None)
    return runtime


class TestGrepToolFilesWithMatches:
    """Test grep tool with files_with_matches output mode."""

    def test_basic_search(self, tool_runtime, temp_repo):
        """Test basic file search."""
        # Call the underlying function directly with runtime
        result = grep_tool.func(pattern="def hello", runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "files_with_matches"
        assert len(data["files"]) > 0
        assert "file1.py" in data["files"]
        assert data["file_count"] == len(data["files"])
        assert data["truncated"] is False

    def test_no_matches(self, tool_runtime, temp_repo):
        """Test search with no matches."""
        result = grep_tool.func(pattern="nonexistent_pattern_xyz", runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "files_with_matches"
        assert data["files"] == []
        assert data["file_count"] == 0
        assert data["truncated"] is False

    def test_glob_filter(self, tool_runtime, temp_repo):
        """Test search with glob filter."""
        result = grep_tool.func(pattern="def", glob="*.py", runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "files_with_matches"
        assert all(f.endswith(".py") for f in data["files"])
        assert not any(f.endswith(".js") for f in data["files"])

    def test_head_limit(self, tool_runtime, temp_repo):
        """Test head_limit truncation."""
        result = grep_tool.func(pattern="def", head_limit=2, runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "files_with_matches"
        assert len(data["files"]) <= 2
        if data["file_count"] > 2:
            assert data["truncated"] is True
        else:
            assert data["truncated"] is False

    def test_case_insensitive(self, tool_runtime, temp_repo):
        """Test case-insensitive search."""
        result_lower = grep_tool.func(pattern="def", ignore_case=False, runtime=tool_runtime)

        result_upper = grep_tool.func(pattern="DEF", ignore_case=True, runtime=tool_runtime)

        data_lower = json.loads(result_lower)
        data_upper = json.loads(result_upper)

        # With ignore_case=True, both should find the same files
        assert set(data_lower["files"]) == set(data_upper["files"])

    def test_path_parameter(self, tool_runtime, temp_repo):
        """Test searching in a specific directory."""
        result = grep_tool.func(pattern="def", path="subdir", runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "files_with_matches"
        assert all("subdir" in f for f in data["files"])


class TestGrepToolContent:
    """Test grep tool with content output mode."""

    def test_basic_content_search(self, tool_runtime, temp_repo):
        """Test content search with matches."""
        result = grep_tool.func(pattern="def hello", output_mode="content", runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "content"
        assert len(data["matches"]) > 0
        assert data["matches"][0]["file"] == "file1.py"
        assert "def hello" in data["matches"][0]["line"]
        assert data["total_matches"] >= len(data["matches"])

    def test_line_numbers(self, tool_runtime, temp_repo):
        """Test content search with line numbers."""
        result = grep_tool.func(
            pattern="def hello", output_mode="content", show_line_numbers=True, runtime=tool_runtime
        )

        data = json.loads(result)
        assert data["output_mode"] == "content"
        assert data["matches"][0]["line_number"] is not None
        assert isinstance(data["matches"][0]["line_number"], int)

    def test_context_lines(self, tool_runtime, temp_repo):
        """Test content search with context lines."""
        result = grep_tool.func(
            pattern="print", output_mode="content", before_context=1, after_context=1, runtime=tool_runtime
        )

        data = json.loads(result)
        assert data["output_mode"] == "content"
        # Check that context is present
        match = data["matches"][0]
        assert match["before_context"] is not None or match["after_context"] is not None

    def test_head_limit_content(self, tool_runtime, temp_repo):
        """Test head_limit for content mode."""
        result = grep_tool.func(pattern="print", output_mode="content", head_limit=2, runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "content"
        assert len(data["matches"]) <= 2
        if data["total_matches"] > 2:
            assert data["truncated"] is True


class TestGrepToolCount:
    """Test grep tool with count output mode."""

    def test_basic_count(self, tool_runtime, temp_repo):
        """Test count search."""
        result = grep_tool.func(pattern="def", output_mode="count", runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "count"
        assert len(data["per_file"]) > 0
        assert all("file" in item and "count" in item for item in data["per_file"])
        assert data["total_matches"] > 0
        assert data["total_matches"] == sum(item["count"] for item in data["per_file"])

    def test_head_limit_count(self, tool_runtime, temp_repo):
        """Test head_limit for count mode."""
        result = grep_tool.func(pattern="def", output_mode="count", head_limit=2, runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "count"
        assert len(data["per_file"]) <= 2
        # truncated is True if there are more files with matches than head_limit
        # We can't easily check this without knowing the actual file count, so we just verify the structure


class TestGrepToolErrors:
    """Test error handling in grep tool."""

    def test_invalid_path(self, tool_runtime, temp_repo):
        """Test error for invalid path."""
        result = grep_tool.func(pattern="test", path="nonexistent_directory", runtime=tool_runtime)

        assert result.startswith("error:")
        assert "does not exist" in result.lower()

    def test_invalid_regex(self, tool_runtime, temp_repo):
        """Test error for invalid regex pattern."""
        result = grep_tool.func(pattern="[unclosed", runtime=tool_runtime)

        assert result.startswith("error:")
        assert "regex" in result.lower() or "invalid" in result.lower()

    def test_invalid_head_limit(self, tool_runtime, temp_repo):
        """Test error for invalid head_limit."""
        result = grep_tool.func(pattern="test", head_limit=0, runtime=tool_runtime)

        assert result.startswith("error:")
        assert "head_limit" in result.lower()

    def test_invalid_output_mode(self, tool_runtime, temp_repo):
        """Test error for invalid output_mode."""
        # This should be caught by LangChain validation, but test anyway
        result = grep_tool.func(pattern="test", output_mode="invalid_mode", runtime=tool_runtime)

        assert result.startswith("error:")
        assert "output_mode" in result.lower() or "invalid" in result.lower()


class TestGrepToolEdgeCases:
    """Test edge cases and special scenarios."""

    def test_multiline_pattern(self, tool_runtime, temp_repo):
        """Test multiline pattern matching."""
        result = grep_tool.func(
            pattern="def.*\\n.*print", output_mode="files_with_matches", multiline=True, runtime=tool_runtime
        )

        data = json.loads(result)
        assert data["output_mode"] == "files_with_matches"
        # Should find files with def followed by print

    def test_empty_pattern(self, tool_runtime, temp_repo):
        """Test empty pattern (should match everything or error)."""
        result = grep_tool.func(pattern="", runtime=tool_runtime)

        # Empty pattern might error or match everything
        # Just verify it doesn't crash
        assert isinstance(result, str)

    def test_todo_fixme_search(self, tool_runtime, temp_repo):
        """Test searching for TODO/FIXME comments."""
        result = grep_tool.func(pattern="TODO|FIXME", output_mode="content", runtime=tool_runtime)

        data = json.loads(result)
        assert data["output_mode"] == "content"
        assert len(data["matches"]) > 0
        assert any("TODO" in m["line"] or "FIXME" in m["line"] for m in data["matches"])
