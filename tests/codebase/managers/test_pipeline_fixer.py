from unittest.mock import Mock, patch

import pytest

from codebase.base import ClientType, MergeRequestDiff
from codebase.clients import AllRepoClient
from codebase.managers.pipeline_fixer import PipelineFixerManager


@pytest.fixture
@patch("codebase.managers.base.RepositoryConfig", new=Mock())
def pipeline_fixer() -> PipelineFixerManager:
    client = Mock(spec=AllRepoClient)
    return PipelineFixerManager(client, repo_id="test-repo", ref="main", thread_id="test-thread")


def test_clean_logs_gitlab(pipeline_fixer: PipelineFixerManager):
    """Test that _clean_logs properly processes GitLab logs."""
    pipeline_fixer.client.client_slug = ClientType.GITLAB
    raw_log = "section_start:123: step_script\r\nCommand output\r\nError message\r\nsection_end:123: step_script"

    with (
        patch.object(pipeline_fixer, "_clean_gitlab_logs", return_value=raw_log) as mock_clean_gitlab_logs,
        patch.object(
            pipeline_fixer, "_extract_last_command_from_gitlab_logs", return_value=raw_log
        ) as mock_extract_last_command_from_gitlab_logs,
    ):
        result = pipeline_fixer._clean_logs(raw_log)

        mock_clean_gitlab_logs.assert_called_once_with(raw_log)
        mock_extract_last_command_from_gitlab_logs.assert_called_once_with(raw_log)

    assert result == raw_log


def test_clean_logs_non_gitlab(pipeline_fixer: PipelineFixerManager):
    """Test that _clean_logs returns original logs for non-GitLab clients."""
    pipeline_fixer.client.client_slug = ClientType.GITHUB
    raw_log = "Some log content"

    result = pipeline_fixer._clean_logs(raw_log)

    assert result == raw_log


def test_clean_gitlab_logs(pipeline_fixer: PipelineFixerManager):
    """Test that _clean_gitlab_logs properly cleans GitLab logs."""
    raw_log = (
        "\x1b[0msection_start:123: step_script\r\n"
        "Running command\x1b[0m\r\n"
        "Output with\rcarriage return\r\n"
        "\x1b[32mColored text\x1b[0m\n"
        "section_end:123: step_script"
    )

    result = pipeline_fixer._clean_gitlab_logs(raw_log)

    assert "\x1b[" not in result  # No ANSI codes
    assert "\r\n" not in result  # No Windows line endings
    assert ">>> step_script" in result
    assert "<<< step_script" in result
    assert "Running command" in result
    assert "Output with" in result
    assert "carriage return" in result
    assert "Colored text" in result


def test_extract_last_command_from_gitlab_logs(pipeline_fixer: PipelineFixerManager):
    """Test that _extract_last_command_from_gitlab_logs extracts the last command correctly."""
    log = (
        "$ first command\nfirst output\n"
        "$ second command\nsecond output\n<<< step_script\n"
        "$ third command\nthird output\nExit code 1"
    )

    result = pipeline_fixer._extract_last_command_from_gitlab_logs(log)

    assert "$ third command" in result
    assert "third output" in result
    assert "Exit code 1" in result
    assert "first command" not in result
    assert "second command" not in result


def test_extract_last_command_empty_log(pipeline_fixer: PipelineFixerManager):
    """Test that _extract_last_command_from_gitlab_logs handles empty logs."""
    result = pipeline_fixer._extract_last_command_from_gitlab_logs("")
    assert result == ""


def test_merge_request_diffs_to_str(pipeline_fixer: PipelineFixerManager):
    """Test that _merge_request_diffs_to_str properly converts diffs to string."""
    diffs = [
        MergeRequestDiff(
            repo_id="test-repo",
            merge_request_id=1,
            ref="main",
            old_path="file1.py",
            new_path="file1.py",
            diff=b"diff1 content",
        ),
        MergeRequestDiff(
            repo_id="test-repo",
            merge_request_id=1,
            ref="main",
            old_path="file2.py",
            new_path="file2.py",
            diff=b"diff2 content",
        ),
        MergeRequestDiff(
            repo_id="test-repo", merge_request_id=1, ref="main", old_path="file3.py", new_path="file3.py", diff=b""
        ),  # Should be skipped
    ]

    result = pipeline_fixer._merge_request_diffs_to_str(diffs)

    assert "diff1 content" in result
    assert "diff2 content" in result
    assert result == "diff1 content\ndiff2 content"


def test_merge_request_diffs_to_str_empty(pipeline_fixer: PipelineFixerManager):
    """Test that _merge_request_diffs_to_str handles empty diff list."""
    result = pipeline_fixer._merge_request_diffs_to_str([])
    assert result == ""


@patch('daiv.codebase.managers.pipeline_fixer.RepositoryConfig')
def test_is_job_excluded_exact_match(mock_repo_config, pipeline_fixer):
    """Test exact job name matches for exclusion."""
    mock_config = Mock()
    mock_config.pipeline.excluded_job_patterns = ["security-scan", "deploy-prod", "manual-review"]
    mock_repo_config.get_config.return_value = mock_config
    
    assert pipeline_fixer._is_job_excluded("security-scan") is True
    assert pipeline_fixer._is_job_excluded("deploy-prod") is True
    assert pipeline_fixer._is_job_excluded("manual-review") is True
    assert pipeline_fixer._is_job_excluded("test-unit") is False


@patch('daiv.codebase.managers.pipeline_fixer.RepositoryConfig')
def test_is_job_excluded_wildcard_patterns(mock_repo_config, pipeline_fixer):
    """Test wildcard pattern matching for job exclusion."""
    mock_config = Mock()
    mock_config.pipeline.excluded_job_patterns = ["security-*", "*-deploy", "manual-*"]
    mock_repo_config.get_config.return_value = mock_config
    
    # Test various job names against patterns
    assert pipeline_fixer._is_job_excluded("security-scan") is True
    assert pipeline_fixer._is_job_excluded("prod-deploy") is True
    assert pipeline_fixer._is_job_excluded("manual-review") is True
    assert pipeline_fixer._is_job_excluded("test-unit") is False
    assert pipeline_fixer._is_job_excluded("build") is False


@patch('daiv.codebase.managers.pipeline_fixer.RepositoryConfig')
def test_is_job_excluded_no_patterns(mock_repo_config, pipeline_fixer):
    """Test when no exclusion patterns are configured."""
    mock_config = Mock()
    mock_config.pipeline.excluded_job_patterns = []
    mock_repo_config.get_config.return_value = mock_config
    
    assert pipeline_fixer._is_job_excluded("security-scan") is False
    assert pipeline_fixer._is_job_excluded("deploy-prod") is False
    assert pipeline_fixer._is_job_excluded("any-job") is False


@patch('daiv.codebase.managers.pipeline_fixer.RepositoryConfig')
def test_is_job_excluded_no_match(mock_repo_config, pipeline_fixer):
    """Test when job doesn't match any patterns."""
    mock_config = Mock()
    mock_config.pipeline.excluded_job_patterns = ["security-*", "deploy-*"]
    mock_repo_config.get_config.return_value = mock_config
    
    assert pipeline_fixer._is_job_excluded("test-unit") is False
    assert pipeline_fixer._is_job_excluded("build-app") is False
    assert pipeline_fixer._is_job_excluded("lint-code") is False


@patch('daiv.codebase.managers.pipeline_fixer.RepositoryConfig')
@patch('daiv.codebase.managers.pipeline_fixer.logger')
async def test_process_job_excluded(mock_logger, mock_repo_config, pipeline_fixer):
    """Test that excluded jobs are not processed."""
    mock_config = Mock()
    mock_config.pipeline.excluded_job_patterns = ["security-*"]
    mock_repo_config.get_config.return_value = mock_config
    
    # Mock the client methods to ensure they're not called
    pipeline_fixer.client.job_log_trace = Mock()
    pipeline_fixer.client.get_merge_request_diff = Mock()
    
    await pipeline_fixer._process_job(1, 123, "security-scan")
    
    # Verify exclusion was logged
    mock_logger.info.assert_called_once_with(
        "Job '%s' excluded from automatic fixing due to configuration patterns", "security-scan"
    )
    
    # Verify no further processing occurred
    pipeline_fixer.client.job_log_trace.assert_not_called()
    pipeline_fixer.client.get_merge_request_diff.assert_not_called()


@patch('daiv.codebase.managers.pipeline_fixer.RepositoryConfig')
async def test_process_job_not_excluded(mock_repo_config, pipeline_fixer):
    """Test that non-excluded jobs are processed normally."""
    mock_config = Mock()
    mock_config.pipeline.excluded_job_patterns = ["security-*"]
    mock_repo_config.get_config.return_value = mock_config
    
    # Mock the client methods
    pipeline_fixer.client.job_log_trace = Mock(return_value="test logs")
    pipeline_fixer.client.get_merge_request_diff = Mock(return_value=[])
    
    # Mock the agent and other dependencies to avoid full processing
    with patch('daiv.codebase.managers.pipeline_fixer.AsyncPostgresSaver'), \
         patch('daiv.codebase.managers.pipeline_fixer.PipelineFixerAgent'), \
         patch.object(pipeline_fixer, '_get_file_changes', return_value=None), \
         patch('daiv.codebase.managers.pipeline_fixer.RunnableConfig'):
        
        await pipeline_fixer._process_job(1, 123, "test-unit")
    
    # Verify processing started (client methods were called)
    pipeline_fixer.client.job_log_trace.assert_called_once_with("test-repo", 123)
    pipeline_fixer.client.get_merge_request_diff.assert_called_once_with("test-repo", 1)
