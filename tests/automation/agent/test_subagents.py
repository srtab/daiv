"""Tests for deepagent subagents."""

from unittest.mock import Mock, patch

import pytest

from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.subagents import (
    create_changelog_subagent,
    create_explore_subagent,
    create_general_purpose_subagent,
)


class TestGeneralPurposeSubagent:
    """Tests for create_general_purpose_subagent."""

    @pytest.fixture
    def mock_runtime_ctx(self):
        """Create a mock runtime context."""
        mock_ctx = Mock()
        mock_ctx.config.sandbox.enabled = False
        return mock_ctx

    def test_returns_subagent(self, mock_runtime_ctx):
        """Test that create_general_purpose_subagent returns a SubAgent."""
        result = create_general_purpose_subagent(mock_runtime_ctx)

        assert isinstance(result, dict)
        assert result["name"] == "general-purpose"
        assert result["description"]
        assert result["system_prompt"]

    def test_includes_web_search_middleware(self, mock_runtime_ctx):
        """Test that general purpose subagent includes web search middleware."""
        result = create_general_purpose_subagent(mock_runtime_ctx)

        assert any(isinstance(m, WebSearchMiddleware) for m in result["middleware"])

    @patch("automation.agent.middlewares.sandbox.settings.SANDBOX_API_KEY", "test-key")
    def test_includes_sandbox_when_enabled(self, mock_runtime_ctx):
        """Test that sandbox middleware is included when enabled."""
        mock_runtime_ctx.config.sandbox.enabled = True

        result = create_general_purpose_subagent(mock_runtime_ctx)

        sandbox_middlewares = [m for m in result["middleware"] if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_excludes_sandbox_when_disabled(self, mock_runtime_ctx):
        """Test that sandbox middleware is excluded when disabled."""
        mock_runtime_ctx.config.sandbox.enabled = False

        result = create_general_purpose_subagent(mock_runtime_ctx)

        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])


class TestExploreSubagent:
    """Tests for create_explore_subagent."""

    @pytest.fixture
    def mock_runtime_ctx(self):
        """Create a mock runtime context."""
        mock_ctx = Mock()
        mock_ctx.config.sandbox.enabled = False
        return mock_ctx

    def test_returns_subagent(self, mock_runtime_ctx):
        """Test that create_explore_subagent returns a SubAgent."""
        result = create_explore_subagent(mock_runtime_ctx)

        assert isinstance(result, dict)
        assert result["name"] == "explore"
        assert result["description"]
        assert result["system_prompt"]

    def test_system_prompt_mentions_readonly(self, mock_runtime_ctx):
        """Test that explore subagent system prompt mentions read-only mode."""
        result = create_explore_subagent(mock_runtime_ctx)

        assert "READ-ONLY" in result["system_prompt"]
        assert "PROHIBITED" in result["system_prompt"]

    @patch("automation.agent.middlewares.sandbox.settings.SANDBOX_API_KEY", "test-key")
    def test_includes_sandbox_when_enabled(self, mock_runtime_ctx):
        """Test that sandbox middleware is included when enabled."""
        mock_runtime_ctx.config.sandbox.enabled = True

        result = create_explore_subagent(mock_runtime_ctx)

        sandbox_middlewares = [m for m in result["middleware"] if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_excludes_sandbox_when_disabled(self, mock_runtime_ctx):
        """Test that sandbox middleware is excluded when disabled."""
        mock_runtime_ctx.config.sandbox.enabled = False

        result = create_explore_subagent(mock_runtime_ctx)

        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])


class TestChangelogSubagent:
    """Tests for create_changelog_subagent."""

    @pytest.fixture
    def mock_runtime_ctx(self):
        """Create a mock runtime context."""
        mock_ctx = Mock()
        mock_ctx.config.sandbox.enabled = False
        return mock_ctx

    def test_returns_subagent(self, mock_runtime_ctx):
        """Test that create_changelog_subagent returns a SubAgent."""
        result = create_changelog_subagent(mock_runtime_ctx)

        assert isinstance(result, dict)
        assert result["name"] == "changelog"
        assert result["description"]
        assert result["system_prompt"]

    def test_description_mentions_changelog_keywords(self, mock_runtime_ctx):
        """Test that changelog subagent description mentions relevant keywords."""
        result = create_changelog_subagent(mock_runtime_ctx)

        description = result["description"].lower()
        assert "changelog" in description
        assert "release notes" in description

    def test_system_prompt_mentions_discovery(self, mock_runtime_ctx):
        """Test that changelog subagent system prompt mentions discovery."""
        result = create_changelog_subagent(mock_runtime_ctx)

        prompt = result["system_prompt"]
        assert "DISCOVERY" in prompt
        assert "glob" in prompt.lower()

    def test_system_prompt_mentions_format_detection(self, mock_runtime_ctx):
        """Test that changelog subagent system prompt mentions format detection."""
        result = create_changelog_subagent(mock_runtime_ctx)

        prompt = result["system_prompt"]
        assert "FORMAT DETECTION" in prompt
        assert "conventions" in prompt.lower()

    def test_system_prompt_mentions_unreleased_rule(self, mock_runtime_ctx):
        """Test that changelog subagent system prompt mentions unreleased section rule."""
        result = create_changelog_subagent(mock_runtime_ctx)

        prompt = result["system_prompt"]
        assert "UNRELEASED" in prompt
        assert "unreleased" in prompt.lower()

    @patch("automation.agent.middlewares.sandbox.settings.SANDBOX_API_KEY", "test-key")
    def test_includes_sandbox_when_enabled(self, mock_runtime_ctx):
        """Test that sandbox middleware is included when enabled."""
        mock_runtime_ctx.config.sandbox.enabled = True

        result = create_changelog_subagent(mock_runtime_ctx)

        sandbox_middlewares = [m for m in result["middleware"] if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_excludes_sandbox_when_disabled(self, mock_runtime_ctx):
        """Test that sandbox middleware is excluded when disabled."""
        mock_runtime_ctx.config.sandbox.enabled = False

        result = create_changelog_subagent(mock_runtime_ctx)

        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])
