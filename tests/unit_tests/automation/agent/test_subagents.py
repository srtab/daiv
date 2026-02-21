"""Tests for deepagent subagents."""

from unittest.mock import Mock, patch

import pytest
from pydantic import SecretStr

from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.subagents import (
    create_docs_research_subagent,
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
        mock_ctx = Mock()
        mock_ctx.config.sandbox.enabled = False
        return mock_ctx

    def test_returns_subagent(self, mock_model, mock_backend, mock_runtime_ctx):
        """Test that create_general_purpose_subagent returns a SubAgent."""
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx)

        assert isinstance(result, dict)
        assert result["name"] == "general-purpose"
        assert result["description"]
        assert result["system_prompt"]

    def test_includes_web_search_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        """Test that general purpose subagent includes web search middleware."""
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx)

        assert any(isinstance(m, WebSearchMiddleware) for m in result["middleware"])

    @patch("automation.agent.middlewares.sandbox.settings.SANDBOX_API_KEY", "test-key")
    def test_includes_sandbox_when_enabled(self, mock_model, mock_backend, mock_runtime_ctx):
        """Test that sandbox middleware is included when enabled."""
        mock_runtime_ctx.config.sandbox.enabled = True

        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx)

        sandbox_middlewares = [m for m in result["middleware"] if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_excludes_sandbox_when_disabled(self, mock_model, mock_backend, mock_runtime_ctx):
        """Test that sandbox middleware is excluded when disabled."""
        mock_runtime_ctx.config.sandbox.enabled = False

        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx)

        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])


@patch("automation.agent.base.settings.OPENROUTER_API_KEY", SecretStr("test-key"))
class TestExploreSubagent:
    """Tests for create_explore_subagent."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self):
        """Create a mock runtime context."""
        mock_ctx = Mock()
        mock_ctx.config.sandbox.enabled = False
        return mock_ctx

    def test_returns_subagent(self, mock_backend, mock_runtime_ctx):
        """Test that create_explore_subagent returns a SubAgent."""
        result = create_explore_subagent(mock_backend)

        assert isinstance(result, dict)
        assert result["name"] == "explore"
        assert result["description"]
        assert result["system_prompt"]

    def test_system_prompt_mentions_readonly(self, mock_backend, mock_runtime_ctx):
        """Test that explore subagent system prompt mentions read-only mode."""
        result = create_explore_subagent(mock_backend)

        assert "READ-ONLY" in result["system_prompt"]
        assert "PROHIBITED" in result["system_prompt"]

    @patch("automation.agent.middlewares.sandbox.settings.SANDBOX_API_KEY", "test-key")
    def test_excludes_sandbox_when_enabled(self, mock_backend, mock_runtime_ctx):
        """Test that explore subagent never includes sandbox middleware."""
        mock_runtime_ctx.config.sandbox.enabled = True

        result = create_explore_subagent(mock_backend)

        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])

    def test_excludes_sandbox_when_disabled(self, mock_backend, mock_runtime_ctx):
        """Test that sandbox middleware is excluded when disabled."""
        mock_runtime_ctx.config.sandbox.enabled = False

        result = create_explore_subagent(mock_backend)

        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])


@patch("automation.agent.base.settings.OPENROUTER_API_KEY", SecretStr("test-key"))
class TestDocsResearchSubagent:
    """Tests for create_docs_research_subagent."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        return Mock()

    def test_returns_subagent(self, mock_backend):
        """Test that create_docs_research_subagent returns a SubAgent."""
        result = create_docs_research_subagent(mock_backend)

        assert isinstance(result, dict)
        assert result["name"] == "docs-research"
        assert result["description"]
        assert result["system_prompt"]

    def test_description_mentions_docs_keywords(self, mock_backend):
        """Test that docs-research subagent description mentions relevant keywords."""
        result = create_docs_research_subagent(mock_backend)

        description = result["description"].lower()
        assert "documentation" in description

    def test_system_prompt_mentions_context7_and_fetch(self, mock_backend):
        """Test that docs-research subagent system prompt mentions Context7 and fetch."""
        result = create_docs_research_subagent(mock_backend)

        prompt = result["system_prompt"]
        assert "context7" in prompt.lower()
        assert "fetch" in prompt.lower()

    def test_includes_web_fetch_middleware(self, mock_backend):
        """Test that docs-research subagent includes web fetch middleware."""
        result = create_docs_research_subagent(mock_backend)
        assert any(isinstance(m, WebFetchMiddleware) for m in result["middleware"])

    def test_excludes_sandbox_middleware(self, mock_backend):
        """Test that docs-research subagent does not include sandbox middleware."""
        result = create_docs_research_subagent(mock_backend)
        assert not any(isinstance(m, SandboxMiddleware) for m in result["middleware"])
