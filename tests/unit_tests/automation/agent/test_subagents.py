"""Tests for deepagent subagents."""

from unittest.mock import Mock

import pytest

from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.subagents import (
    create_changelog_subagent,
    create_explore_subagent,
    create_general_purpose_subagent,
)


class TestGeneralPurposeSubagent:
    """Tests for create_general_purpose_subagent."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        return Mock()

    @pytest.fixture
    def mock_model(self):
        """Create a mock model."""
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self):
        """Create a mock runtime context."""
        return Mock()

    def test_returns_subagent(self, mock_model, mock_backend, mock_runtime_ctx):
        """Test that create_general_purpose_subagent returns a SubAgent."""
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx)

        assert isinstance(result, dict)
        assert result["name"] == "general-purpose"
        assert result["description"]
        assert result["system_prompt"]
        assert any(isinstance(m, WebFetchMiddleware) for m in result["middleware"])
        assert any(isinstance(m, WebSearchMiddleware) for m in result["middleware"])
        sandbox_middlewares = [m for m in result["middleware"] if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_excludes_sandbox_when_disabled(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx, sandbox_enabled=False)
        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])

    def test_excludes_web_search_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx, web_search_enabled=False)
        assert not any(isinstance(m, WebSearchMiddleware) for m in result["middleware"])

    def test_excludes_web_fetch_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx, web_fetch_enabled=False)
        assert not any(isinstance(m, WebFetchMiddleware) for m in result["middleware"])


class TestExploreSubagent:
    """Tests for create_explore_subagent."""

    def test_returns_subagent(self):
        """Test that create_explore_subagent returns a SubAgent."""
        result = create_explore_subagent(Mock(), Mock())

        assert isinstance(result, dict)
        assert result["name"] == "explore"
        assert result["description"]
        assert result["system_prompt"]
        assert "READ-ONLY" in result["system_prompt"]
        assert "PROHIBITED" in result["system_prompt"]


class TestChangelogSubagent:
    """Tests for create_changelog_subagent."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        return Mock()

    @pytest.fixture
    def mock_model(self):
        """Create a mock model."""
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self):
        """Create a mock runtime context."""
        return Mock()

    def test_returns_subagent(self, mock_model, mock_backend, mock_runtime_ctx):
        """Test that create_changelog_subagent returns a SubAgent."""
        result = create_changelog_subagent(mock_model, mock_backend, mock_runtime_ctx)

        assert isinstance(result, dict)
        assert result["name"] == "changelog-curator"
        assert result["description"]
        assert result["system_prompt"]
        description = result["description"].lower()
        assert "changelog" in description

    def test_includes_sandbox_when_enabled(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_changelog_subagent(mock_model, mock_backend, mock_runtime_ctx, sandbox_enabled=True)

        sandbox_middlewares = [m for m in result["middleware"] if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_excludes_sandbox_when_disabled(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_changelog_subagent(mock_model, mock_backend, mock_runtime_ctx, sandbox_enabled=False)

        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])

    def test_includes_web_search_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_changelog_subagent(mock_model, mock_backend, mock_runtime_ctx, web_search_enabled=True)
        assert any(isinstance(m, WebSearchMiddleware) for m in result["middleware"])

    def test_excludes_web_search_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_changelog_subagent(mock_model, mock_backend, mock_runtime_ctx, web_search_enabled=False)
        assert not any(isinstance(m, WebSearchMiddleware) for m in result["middleware"])
