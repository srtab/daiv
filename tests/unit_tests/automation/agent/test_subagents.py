"""Tests for deepagent subagents."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents.middleware import ModelFallbackMiddleware

from automation.agent.middlewares.file_system import FilesystemMiddleware
from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.subagents import create_explore_subagent, create_general_purpose_subagent, load_custom_subagents

if TYPE_CHECKING:
    from pathlib import Path


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

    def test_includes_fallback_middleware_when_fallback_models_provided(
        self, mock_model, mock_backend, mock_runtime_ctx
    ):
        fallback = [Mock(), Mock()]
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx, fallback_models=fallback)
        assert any(isinstance(m, ModelFallbackMiddleware) for m in result["middleware"])

    def test_excludes_fallback_middleware_when_no_fallback_models(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx)
        assert not any(isinstance(m, ModelFallbackMiddleware) for m in result["middleware"])


class TestExploreSubagent:
    """Tests for create_explore_subagent."""

    def test_returns_subagent(self):
        """Test that create_explore_subagent returns a SubAgent."""
        result = create_explore_subagent(Mock())

        assert isinstance(result, dict)
        assert result["name"] == "explore"
        assert result["description"]
        assert result["system_prompt"]
        assert "READ-ONLY" in result["system_prompt"]
        assert "PROHIBITED" in result["system_prompt"]

    def test_includes_fallback_middleware_when_setting_configured(self, mocker):
        mocker.patch(
            "automation.agent.subagents.site_settings",
            agent_explore_model_name="openrouter:anthropic/claude-haiku-4.5",
            agent_explore_fallback_model_name="openrouter:openai/gpt-5.4-mini",
        )
        result = create_explore_subagent(Mock())
        assert any(isinstance(m, ModelFallbackMiddleware) for m in result["middleware"])

    def test_excludes_fallback_middleware_when_setting_is_none(self, mocker):
        mocker.patch(
            "automation.agent.subagents.site_settings",
            agent_explore_model_name="openrouter:anthropic/claude-haiku-4.5",
            agent_explore_fallback_model_name=None,
        )
        result = create_explore_subagent(Mock())
        assert not any(isinstance(m, ModelFallbackMiddleware) for m in result["middleware"])

    def test_proceeds_without_fallback_on_invalid_model(self, mocker):
        mocker.patch(
            "automation.agent.subagents.site_settings",
            agent_explore_model_name="openrouter:anthropic/claude-haiku-4.5",
            agent_explore_fallback_model_name="totally-invalid-model",
        )
        result = create_explore_subagent(Mock())
        assert not any(isinstance(m, ModelFallbackMiddleware) for m in result["middleware"])


def _make_subagent_md(*, name: str, description: str, model: str | None = None, body: str = "You are a custom agent."):
    lines = ["---", f"name: {name}", f"description: {description}"]
    if model:
        lines.append(f"model: {model}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


class TestCustomSubagents:
    """Tests for load_custom_subagents."""

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self):
        return Mock()

    async def test_loads_custom_subagent(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(
            _make_subagent_md(name="my-agent", description="Does custom things", body="You do custom things.")
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"
        assert result[0]["description"] == "Does custom things"
        assert result[0]["system_prompt"] == "You do custom things."
        assert result[0]["model"] is mock_model

    async def test_loads_multiple_subagents(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "agent-a.md").write_text(_make_subagent_md(name="agent-a", description="Agent A"))
        (subagents_dir / "agent-b.md").write_text(_make_subagent_md(name="agent-b", description="Agent B"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        names = {s["name"] for s in result}
        assert names == {"agent-a", "agent-b"}

    async def test_skips_non_md_files(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))
        (subagents_dir / "readme.txt").write_text("Not a subagent")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"

    async def test_skips_directories(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))
        (subagents_dir / "some-dir").mkdir()

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"

    async def test_skips_missing_name(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad.md").write_text("---\ndescription: no name\n---\nBody here.")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_skips_missing_description(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad.md").write_text("---\nname: bad\n---\nBody here.")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_skips_empty_body(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "empty.md").write_text("---\nname: empty\ndescription: empty body\n---\n")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_skips_no_frontmatter(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "plain.md").write_text("Just some markdown without frontmatter.")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_uses_frontmatter_model(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "custom.md").write_text(
            _make_subagent_md(name="custom", description="Custom agent", model="openrouter:anthropic/claude-haiku-4.5")
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        # Model should not be the default mock_model since frontmatter specifies one
        assert result[0]["model"] is not mock_model

    async def test_falls_back_to_default_model(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "custom.md").write_text(_make_subagent_md(name="custom", description="Custom agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        assert result[0]["model"] is mock_model

    async def test_returns_empty_when_no_source_exists(self, mock_model, mock_runtime_ctx):
        backend = Mock()
        backend.als = AsyncMock(side_effect=FileNotFoundError("not found"))

        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert result == []

    async def test_has_general_purpose_middleware(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "custom.md").write_text(_make_subagent_md(name="custom", description="Custom agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        middleware = result[0]["middleware"]
        assert any(isinstance(m, FilesystemMiddleware) for m in middleware)
        assert any(isinstance(m, GitPlatformMiddleware) for m in middleware)
        assert any(isinstance(m, WebSearchMiddleware) for m in middleware)
        assert any(isinstance(m, WebFetchMiddleware) for m in middleware)
        sandbox_middlewares = [m for m in middleware if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    async def test_excludes_optional_middleware_when_disabled(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "custom.md").write_text(_make_subagent_md(name="custom", description="Custom agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            sandbox_enabled=False,
            web_search_enabled=False,
            web_fetch_enabled=False,
        )

        assert len(result) == 1
        middleware = result[0]["middleware"]
        assert not any(isinstance(m, SandboxMiddleware) for m in middleware)
        assert not any(isinstance(m, WebSearchMiddleware) for m in middleware)
        assert not any(isinstance(m, WebFetchMiddleware) for m in middleware)

    @pytest.mark.parametrize("reserved_name", ["general-purpose", "explore"])
    async def test_skips_builtin_name_collision(self, tmp_path: Path, mock_model, mock_runtime_ctx, reserved_name):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / f"{reserved_name}.md").write_text(
            _make_subagent_md(name=reserved_name, description="Trying to override a built-in subagent")
        )
        (subagents_dir / "custom.md").write_text(_make_subagent_md(name="custom", description="Custom agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        names = {s["name"] for s in result}
        assert reserved_name not in names
        assert "custom" in names

    async def test_includes_fallback_middleware_when_fallback_models_provided(
        self, tmp_path: Path, mock_model, mock_runtime_ctx
    ):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "custom.md").write_text(_make_subagent_md(name="custom", description="Custom agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            fallback_models=[Mock()],
        )

        assert len(result) == 1
        assert any(isinstance(m, ModelFallbackMiddleware) for m in result[0]["middleware"])

    async def test_skips_invalid_model(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad-model.md").write_text(
            _make_subagent_md(name="bad-model", description="Has invalid model", model="totally-invalid-model")
        )
        (subagents_dir / "good.md").write_text(_make_subagent_md(name="good", description="Good agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        names = {s["name"] for s in result}
        assert "bad-model" not in names
        assert "good" in names
