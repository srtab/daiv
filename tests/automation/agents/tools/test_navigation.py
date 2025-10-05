from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from daiv.automation.agents.tools.navigation import read_tool


@pytest.fixture
def temp_repo():
    """Create a temporary repository structure for testing."""
    with TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        yield repo_path


def create_test_file(repo_path: Path, filename: str, num_lines: int) -> Path:
    """Helper function to create a test file with numbered lines."""
    file_path = repo_path / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"Line {i}" for i in range(1, num_lines + 1))
    file_path.write_text(content)
    return file_path


@pytest.mark.asyncio
class TestReadTool:
    """Tests for the read_tool function with chunking functionality."""

    async def test_basic_file_reading(self, temp_repo, monkeypatch):
        """Test basic file reading with a small file (< 2000 lines)."""
        from codebase.context import RepositoryContext

        # Create a small test file
        create_test_file(temp_repo, "small_file.txt", 10)

        # Mock the repository context
        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        result = await read_tool("small_file.txt")

        # Verify all lines are returned
        lines = result.split("\n")
        assert len(lines) == 10
        assert lines[0] == "1: Line 1"
        assert lines[9] == "10: Line 10"

    async def test_default_truncation(self, temp_repo, monkeypatch):
        """Test default truncation with a file > 2000 lines."""
        from codebase.context import RepositoryContext

        # Create a large test file
        create_test_file(temp_repo, "large_file.txt", 2500)

        # Mock the repository context
        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        result = await read_tool("large_file.txt")

        # Verify only first 2000 lines are returned
        lines = result.split("\n")
        # Should have 2000 lines + 2 blank lines + 1 truncation message
        assert "Content truncated" in result
        assert "showing lines 1-2000 of 2500 total lines" in result

        # Verify first and last line of the chunk
        assert lines[0] == "1: Line 1"
        assert lines[1999] == "2000: Line 2000"

    async def test_chunking_with_start_line(self, temp_repo, monkeypatch):
        """Test chunking with start_line parameter."""
        from codebase.context import RepositoryContext

        # Create a test file with 100 lines
        create_test_file(temp_repo, "medium_file.txt", 100)

        # Mock the repository context
        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        # Read lines 20-40 (0-indexed, so start_line=20 means line 21)
        result = await read_tool("medium_file.txt", start_line=20, max_lines=20)

        lines = result.split("\n")
        # Should have 20 lines
        assert len(lines) == 20

        # Verify line numbers are 21-40
        assert lines[0] == "21: Line 21"
        assert lines[19] == "40: Line 40"

        # Verify content matches expected lines
        for i, line in enumerate(lines):
            expected_line_num = 21 + i
            assert line == f"{expected_line_num}: Line {expected_line_num}"

    async def test_chunking_at_end_of_file(self, temp_repo, monkeypatch):
        """Test chunking at the end of file."""
        from codebase.context import RepositoryContext

        # Create a test file with 100 lines
        create_test_file(temp_repo, "end_file.txt", 100)

        # Mock the repository context
        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        # Read from line 90 with max_lines=20
        result = await read_tool("end_file.txt", start_line=90, max_lines=20)

        lines = result.split("\n")
        # Should only have 10 lines (91-100)
        assert len(lines) == 10

        # Verify line numbers are 91-100
        assert lines[0] == "91: Line 91"
        assert lines[9] == "100: Line 100"

        # Verify no truncation message
        assert "Content truncated" not in result

    async def test_parameter_validation_negative_start_line(self, temp_repo, monkeypatch):
        """Test that start_line < 0 returns error."""
        from codebase.context import RepositoryContext

        create_test_file(temp_repo, "test.txt", 10)

        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        result = await read_tool("test.txt", start_line=-1)
        assert "error: start_line must be non-negative" in result

    async def test_parameter_validation_zero_max_lines(self, temp_repo, monkeypatch):
        """Test that max_lines <= 0 returns error."""
        from codebase.context import RepositoryContext

        create_test_file(temp_repo, "test.txt", 10)

        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        result = await read_tool("test.txt", max_lines=0)
        assert "error: max_lines must be positive" in result

        result = await read_tool("test.txt", max_lines=-5)
        assert "error: max_lines must be positive" in result

    async def test_parameter_validation_start_line_beyond_file(self, temp_repo, monkeypatch):
        """Test that start_line >= total_lines returns appropriate message."""
        from codebase.context import RepositoryContext

        create_test_file(temp_repo, "test.txt", 10)

        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        result = await read_tool("test.txt", start_line=10)
        assert "error: start_line (10) is beyond the file length (10 lines)" in result

        result = await read_tool("test.txt", start_line=100)
        assert "error: start_line (100) is beyond the file length (10 lines)" in result

    async def test_empty_file(self, temp_repo, monkeypatch):
        """Test with empty file."""
        from codebase.context import RepositoryContext

        # Create an empty file
        empty_file = temp_repo / "empty.txt"
        empty_file.write_text("")

        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        result = await read_tool("empty.txt")
        assert "warning: The file exists but is empty" in result

    async def test_non_existent_file(self, temp_repo, monkeypatch):
        """Test with non-existent file."""
        from codebase.context import RepositoryContext

        ctx = RepositoryContext(repo_dir=temp_repo, config=type('obj', (object,), {
            'combined_exclude_patterns': [],
            'omit_content_patterns': []
        })())
        monkeypatch.setattr("daiv.automation.agents.tools.navigation.get_repository_ctx", lambda: ctx)

        result = await read_tool("non_existent.txt")
        assert "error: File 'non_existent.txt' does not exist or is not a file" in result
