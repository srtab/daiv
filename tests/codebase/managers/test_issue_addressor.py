from unittest.mock import Mock

import pytest

from automation.agents.plan_and_execute.conf import settings as plan_and_execute_settings
from codebase.base import Issue, User
from codebase.managers.issue_addressor import IssueAddressorManager


@pytest.fixture
def mock_runtime_ctx(mock_repo_client):
    """Create a mock RuntimeCtx for testing."""
    ctx = Mock()
    ctx.repo_id = "test/repo"
    ctx.bot_username = "daiv-bot"
    ctx.repo = Mock()
    ctx.repo.working_dir = "/tmp/test-repo"  # noqa: S108
    ctx.config = Mock()
    ctx.config.omit_content_patterns = []
    return ctx


class TestIssueAddressorManagerGetAgentKwargs:
    """Test the _get_agent_kwargs() method for IssueAddressorManager."""

    def test_get_agent_kwargs_without_special_labels(self, mock_runtime_ctx, mock_repo_client):
        """Test that _get_agent_kwargs returns empty dict when issue has no special labels."""
        issue = Issue(
            id=1, iid=1, title="Test Issue", author=User(id=2, username="user", name="User"), labels=["bug", "feature"]
        )
        mock_repo_client.get_issue.return_value = issue

        manager = IssueAddressorManager(issue_iid=1, runtime_ctx=mock_runtime_ctx)
        kwargs = manager._get_agent_kwargs()

        assert kwargs == {}

    def test_get_agent_kwargs_with_daiv_auto_label(self, mock_runtime_ctx, mock_repo_client):
        """Test that _get_agent_kwargs does not set skip_approval when issue has daiv-auto label."""
        issue = Issue(
            id=1, iid=1, title="Test Issue", author=User(id=2, username="user", name="User"), labels=["daiv-auto"]
        )
        mock_repo_client.get_issue.return_value = issue

        manager = IssueAddressorManager(issue_iid=1, runtime_ctx=mock_runtime_ctx)
        kwargs = manager._get_agent_kwargs()

        # Note: skip_approval is not in kwargs as it's handled in plan_issue
        assert "skip_approval" not in kwargs

    def test_get_agent_kwargs_with_daiv_max_label(self, mock_runtime_ctx, mock_repo_client):
        """Test that _get_agent_kwargs sets high-performance mode when issue has daiv-max label."""
        issue = Issue(
            id=1, iid=1, title="Test Issue", author=User(id=2, username="user", name="User"), labels=["daiv-max"]
        )
        mock_repo_client.get_issue.return_value = issue

        manager = IssueAddressorManager(issue_iid=1, runtime_ctx=mock_runtime_ctx)
        kwargs = manager._get_agent_kwargs()

        assert kwargs["planning_model_names"] == [
            plan_and_execute_settings.MAX_PLANNING_MODEL_NAME,
            plan_and_execute_settings.PLANNING_MODEL_NAME,
            plan_and_execute_settings.PLANNING_FALLBACK_MODEL_NAME,
        ]
        assert kwargs["execution_model_names"] == [
            plan_and_execute_settings.MAX_EXECUTION_MODEL_NAME,
            plan_and_execute_settings.EXECUTION_MODEL_NAME,
            plan_and_execute_settings.EXECUTION_FALLBACK_MODEL_NAME,
        ]
        assert kwargs["planning_thinking_level"] == plan_and_execute_settings.MAX_PLANNING_THINKING_LEVEL
        assert kwargs["execution_thinking_level"] == plan_and_execute_settings.MAX_EXECUTION_THINKING_LEVEL

    def test_get_agent_kwargs_with_both_labels(self, mock_runtime_ctx, mock_repo_client):
        """Test that _get_agent_kwargs sets both configurations when issue has both labels."""
        issue = Issue(
            id=1,
            iid=1,
            title="Test Issue",
            author=User(id=2, username="user", name="User"),
            labels=["daiv-auto", "daiv-max"],
        )
        mock_repo_client.get_issue.return_value = issue

        manager = IssueAddressorManager(issue_iid=1, runtime_ctx=mock_runtime_ctx)
        kwargs = manager._get_agent_kwargs()

        # skip_approval is handled separately in plan_issue
        assert "skip_approval" not in kwargs
        # But max mode should be set
        assert kwargs["planning_model_names"] == [
            plan_and_execute_settings.MAX_PLANNING_MODEL_NAME,
            plan_and_execute_settings.PLANNING_MODEL_NAME,
            plan_and_execute_settings.PLANNING_FALLBACK_MODEL_NAME,
        ]
        assert kwargs["planning_thinking_level"] == plan_and_execute_settings.MAX_PLANNING_THINKING_LEVEL

    def test_get_agent_kwargs_case_insensitive(self, mock_runtime_ctx, mock_repo_client):
        """Test that _get_agent_kwargs works case-insensitively."""
        issue = Issue(
            id=1, iid=1, title="Test Issue", author=User(id=2, username="user", name="User"), labels=["DAIV-MAX"]
        )
        mock_repo_client.get_issue.return_value = issue

        manager = IssueAddressorManager(issue_iid=1, runtime_ctx=mock_runtime_ctx)
        kwargs = manager._get_agent_kwargs()

        assert "planning_model_names" in kwargs
        assert kwargs["planning_thinking_level"] == plan_and_execute_settings.MAX_PLANNING_THINKING_LEVEL
