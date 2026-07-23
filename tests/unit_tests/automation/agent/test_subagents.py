"""Tests for deepagent subagents.

After migrating to ``create_deep_agent``, the public factories return
``CompiledSubAgent`` dicts (``{name, description, runnable}``) — the middleware
stack is baked into the runnable. Middleware-composition tests therefore
exercise ``_build_general_purpose_middleware`` directly rather than introspect
the compiled runnable, which keeps coverage focused on DAIV's choices about
which middlewares to compose.
"""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import ModelFallbackMiddleware

from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.subagents import (
    _build_detector_middleware,
    _build_general_purpose_middleware,
    create_explore_subagent,
    create_general_purpose_subagent,
    load_custom_subagents,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestGeneralPurposeMiddleware:
    """Tests for ``_build_general_purpose_middleware`` — the middleware composer."""

    @pytest.fixture
    def mock_backend(self):
        return Mock()

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        ctx = Mock()
        ctx.gitrepo.working_dir = str(repo_dir)
        return ctx

    def test_includes_full_stack_by_default(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
        )
        assert any(isinstance(m, FilesystemMiddleware) for m in middleware)
        assert any(isinstance(m, GitPlatformMiddleware) for m in middleware)
        assert any(isinstance(m, WebFetchMiddleware) for m in middleware)
        assert any(isinstance(m, WebSearchMiddleware) for m in middleware)
        sandbox_middlewares = [m for m in middleware if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_threads_client_and_sandbox_backend_into_sandbox_middleware(
        self, mock_model, mock_backend, mock_runtime_ctx
    ):
        """The run-scoped client and the parent's bound backend must reach the subagent's
        SandboxMiddleware: the subagent's bash tool runs through the shared backend, so a dropped
        argument would make that bash raise ``...bound the sandbox backend`` at runtime."""
        sentinel_client = Mock()
        sentinel_backend = Mock()
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
            client=sentinel_client,
            sandbox_backend=sentinel_backend,
        )
        sandbox_mw = next(m for m in middleware if isinstance(m, SandboxMiddleware))
        assert sandbox_mw._client is sentinel_client
        assert sandbox_mw._sandbox_backend is sentinel_backend

    def test_excludes_sandbox_when_disabled(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=False,
            web_search_enabled=True,
            web_fetch_enabled=True,
        )
        assert not any(isinstance(m, SandboxMiddleware) for m in middleware)

    def test_excludes_web_search_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=False,
            web_fetch_enabled=True,
        )
        assert not any(isinstance(m, WebSearchMiddleware) for m in middleware)

    def test_excludes_web_fetch_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=False,
        )
        assert not any(isinstance(m, WebFetchMiddleware) for m in middleware)

    def test_includes_fallback_middleware_when_fallback_models_provided(
        self, mock_model, mock_backend, mock_runtime_ctx
    ):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
            fallback_models=[Mock(), Mock()],
        )
        assert any(isinstance(m, ModelFallbackMiddleware) for m in middleware)

    def test_excludes_fallback_middleware_when_no_fallback_models(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
        )
        assert not any(isinstance(m, ModelFallbackMiddleware) for m in middleware)

    def test_disk_mode_applies_workspace_fence(self, mock_model, mock_backend, mock_runtime_ctx):
        from deepagents.middleware.filesystem import FilesystemMiddleware

        from automation.agent.middlewares.file_system import WORKSPACE_FENCE_PERMISSIONS

        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=False,
            web_search_enabled=False,
            web_fetch_enabled=False,
        )
        fs = next(m for m in middleware if isinstance(m, FilesystemMiddleware))
        assert fs._permissions == WORKSPACE_FENCE_PERMISSIONS

    def test_sandbox_mode_has_no_fence(self, mock_model, mock_backend, mock_runtime_ctx):
        from deepagents.middleware.filesystem import FilesystemMiddleware

        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=False,
            web_fetch_enabled=False,
        )
        fs = next(m for m in middleware if isinstance(m, FilesystemMiddleware))
        assert fs._permissions == []

    def test_includes_loop_breaker_with_error_terminal(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
        )
        breakers = [m for m in middleware if isinstance(m, LoopBreakerMiddleware)]
        assert len(breakers) == 1
        assert breakers[0].terminal == "error"

    def test_detector_stack_includes_loop_breaker_with_error_terminal(self, mock_model, mock_backend):
        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=True, name="cr-correctness")
        breakers = [m for m in middleware if isinstance(m, LoopBreakerMiddleware)]
        assert len(breakers) == 1
        assert breakers[0].terminal == "error"

    def test_subagents_loop_breaker_registered_before_prompt_caching(self):
        import inspect

        from automation.agent import subagents as subagents_module

        src = inspect.getsource(subagents_module)
        breaker = src.index("LoopBreakerMiddleware(terminal=")
        caching = src.index("AnthropicPromptCachingMiddleware()")
        assert breaker < caching


class TestGeneralPurposeSubagent:
    """Tests for the public ``create_general_purpose_subagent`` factory."""

    @pytest.fixture
    def mock_backend(self):
        return Mock()

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        ctx = Mock()
        ctx.gitrepo.working_dir = str(repo_dir)
        return ctx

    def test_returns_compiled_subagent(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx, "/workspace/repo/")

        assert isinstance(result, dict)
        assert result["name"] == "general-purpose"
        assert result["description"]
        assert "runnable" in result

    def test_prompt_names_the_working_directory(self):
        """The whole point of threading working_directory: the subagent prompt must embed it (and
        drop the stale /repo/ example), so the model addresses files under the right root. A revert
        to a static prompt would silently regress this."""
        from automation.agent.subagents import _general_purpose_system_prompt

        prompt = _general_purpose_system_prompt("/workspace/repo/")
        assert "/workspace/repo/" in prompt
        assert "e.g., /workspace/repo/src/app/utils.py" in prompt  # example is rooted at the workspace
        assert "e.g., /repo/src" not in prompt  # not the stale bare-/repo/ example


class TestSubagentMcpTools:
    """MCP tools must reach the general-purpose and custom subagents.

    Without this, a ``task`` delegation that calls an MCP tool (e.g. ``rt_search_tickets``)
    fails — the tool isn't in the subagent's registry, so the model tries it as a shell
    command and gets ``command not found``. The subagent mirrors the main agent: MCP tools
    are deferred behind ``tool_search`` when deferral is on, bound directly when it's off.
    """

    @pytest.fixture
    def mock_backend(self):
        return Mock()

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        ctx = Mock()
        ctx.gitrepo.working_dir = str(repo_dir)
        return ctx

    @pytest.fixture
    def mcp_tool(self):
        from langchain_core.tools import StructuredTool

        return StructuredTool.from_function(func=lambda **k: "x", name="rt_search_tickets", description="Search RT")

    def test_builder_defers_mcp_tools_when_deferral_enabled(self, mock_model, mock_backend, mock_runtime_ctx, mcp_tool):
        from automation.agent.middlewares.deferred_tools import DeferredToolsMiddleware
        from automation.agent.subagents import SUBAGENT_ALWAYS_LOADED_TOOLS

        with patch("automation.agent.middlewares.deferred_tools.deferred_settings") as ds:
            ds.ENABLED = True
            ds.TOP_K_DEFAULT = 3
            ds.TOP_K_MAX = 10
            middleware = _build_general_purpose_middleware(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                sandbox_enabled=True,
                web_search_enabled=True,
                web_fetch_enabled=True,
                mcp_tools=[mcp_tool],
            )

        dtm = next(m for m in middleware if isinstance(m, DeferredToolsMiddleware))
        assert dtm._extra_tools == [mcp_tool]
        # The subagent's own tools stay always-loaded — only MCP tools are deferred.
        assert dtm._always_loaded >= SUBAGENT_ALWAYS_LOADED_TOOLS

    def test_builder_still_installs_deferred_middleware_without_mcp_tools(
        self, mock_model, mock_backend, mock_runtime_ctx
    ):
        # Even with no MCP tools, a subagent still gets DeferredToolsMiddleware when deferral is on:
        # it defers the subagent's own web search/fetch + git-platform tools (not in
        # SUBAGENT_ALWAYS_LOADED_TOOLS), mirroring the main agent.
        from automation.agent.middlewares.deferred_tools import DeferredToolsMiddleware

        with patch("automation.agent.middlewares.deferred_tools.deferred_settings") as ds:
            ds.ENABLED = True
            ds.TOP_K_DEFAULT = 3
            ds.TOP_K_MAX = 10
            middleware = _build_general_purpose_middleware(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                sandbox_enabled=True,
                web_search_enabled=True,
                web_fetch_enabled=True,
                mcp_tools=[],
            )

        dtm = next(m for m in middleware if isinstance(m, DeferredToolsMiddleware))
        assert dtm._extra_tools == []

    def test_builder_omits_deferred_middleware_when_deferral_disabled(
        self, mock_model, mock_backend, mock_runtime_ctx, mcp_tool
    ):
        from automation.agent.middlewares.deferred_tools import DeferredToolsMiddleware

        with patch("automation.agent.middlewares.deferred_tools.deferred_settings") as ds:
            ds.ENABLED = False
            middleware = _build_general_purpose_middleware(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                sandbox_enabled=True,
                web_search_enabled=True,
                web_fetch_enabled=True,
                mcp_tools=[mcp_tool],
            )

        assert not any(isinstance(m, DeferredToolsMiddleware) for m in middleware)

    def test_web_git_and_mcp_deferred_file_bash_core_stays_loaded(
        self, mock_model, mock_backend, mock_runtime_ctx, mcp_tool
    ):
        # Subagents mirror the main agent: the file/bash/todo core stays eagerly bound, while web
        # search/fetch, git-platform, and MCP tools all fall behind tool_search.
        from langchain_core.tools import StructuredTool

        from automation.agent.middlewares.deferred_tools import DeferredToolsMiddleware

        native = [
            StructuredTool.from_function(func=lambda **k: "", name=n, description=n)
            for n in ("read_file", "bash", "web_search", "gitlab")
        ]
        with patch("automation.agent.middlewares.deferred_tools.deferred_settings") as ds:
            ds.ENABLED = True
            ds.TOP_K_DEFAULT = 3
            ds.TOP_K_MAX = 10
            middleware = _build_general_purpose_middleware(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                sandbox_enabled=True,
                web_search_enabled=True,
                web_fetch_enabled=True,
                mcp_tools=[mcp_tool],
            )

        dtm = next(m for m in middleware if isinstance(m, DeferredToolsMiddleware))
        deferred = {e.name for e in dtm._build_index(native).deferred_entries()}
        assert deferred == {"web_search", "gitlab", "rt_search_tickets"}  # web/git + MCP deferred
        assert "read_file" not in deferred and "bash" not in deferred  # file/bash core stays loaded

    def test_general_purpose_binds_mcp_tools_directly_when_deferral_disabled(
        self, mock_model, mock_backend, mock_runtime_ctx, mcp_tool
    ):
        with (
            patch("automation.agent.middlewares.deferred_tools.deferred_settings") as ds,
            patch("automation.agent.subagents.create_agent") as mock_create,
        ):
            ds.ENABLED = False
            mock_create.return_value = Mock()
            create_general_purpose_subagent(
                mock_model, mock_backend, mock_runtime_ctx, "/workspace/repo/", mcp_tools=[mcp_tool]
            )

        assert mock_create.call_args.kwargs["tools"] == [mcp_tool]

    def test_general_purpose_passes_empty_tools_when_deferral_enabled(
        self, mock_model, mock_backend, mock_runtime_ctx, mcp_tool
    ):
        # Deferral on: the MCP tools ride on DeferredToolsMiddleware, so create_agent gets none
        # directly (the middleware already registers them; binding them directly too is redundant).
        with (
            patch("automation.agent.middlewares.deferred_tools.deferred_settings") as ds,
            patch("automation.agent.subagents.create_agent") as mock_create,
        ):
            ds.ENABLED = True
            ds.TOP_K_DEFAULT = 3
            ds.TOP_K_MAX = 10
            mock_create.return_value = Mock()
            create_general_purpose_subagent(
                mock_model, mock_backend, mock_runtime_ctx, "/workspace/repo/", mcp_tools=[mcp_tool]
            )

        assert mock_create.call_args.kwargs["tools"] == []

    async def test_custom_subagents_receive_mcp_tools(self, tmp_path, mock_model, mock_runtime_ctx, mcp_tool):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        with patch("automation.agent.subagents._build_general_purpose_middleware", return_value=[]) as build_mw:
            await load_custom_subagents(
                model=mock_model,
                backend=backend,
                runtime=mock_runtime_ctx,
                sources=["/repo/.agents/subagents"],
                working_directory="/workspace/repo/",
                mcp_tools=[mcp_tool],
            )

        build_mw.assert_called_once()
        assert build_mw.call_args.kwargs["mcp_tools"] == [mcp_tool]

    async def test_custom_subagents_bind_mcp_tools_directly_when_deferral_disabled(
        self, tmp_path, mock_model, mock_runtime_ctx, mcp_tool
    ):
        # Custom subagents are a distinct create_agent call site (_compile_subagent), so the
        # deferral-off direct-bind path needs its own coverage alongside the general-purpose one.
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        with (
            patch("automation.agent.middlewares.deferred_tools.deferred_settings") as ds,
            patch("automation.agent.subagents.create_agent") as mock_create,
        ):
            ds.ENABLED = False
            mock_create.return_value = Mock()
            await load_custom_subagents(
                model=mock_model,
                backend=backend,
                runtime=mock_runtime_ctx,
                sources=["/repo/.agents/subagents"],
                working_directory="/workspace/repo/",
                mcp_tools=[mcp_tool],
            )

        assert mock_create.call_args.kwargs["tools"] == [mcp_tool]

    def test_direct_mcp_tools_returns_empty_for_none_or_empty(self):
        # ``mcp_tools`` defaults to None on every subagent factory, so the helper must guard None/[]
        # before ``list(mcp_tools)`` — a caller that omits MCP tools never hits an AttributeError.
        # Both cases return [] regardless of the deferral flag (the ``not mcp_tools`` guard fires first).
        from automation.agent.middlewares.deferred_tools import direct_mcp_tools

        assert direct_mcp_tools(None) == []
        assert direct_mcp_tools([]) == []


@pytest.mark.django_db
class TestExploreSubagent:
    """Tests for the public ``create_explore_subagent`` factory."""

    def test_returns_compiled_subagent(self):
        from core.models import Provider

        # ``BaseAgent.get_model`` resolves model_name → Provider row → live client; enable
        # the seed row backing ``ModelName.CLAUDE_HAIKU_4_5`` (openrouter:anthropic/...)
        # so the call doesn't error during init_chat_model.
        p = Provider.objects.get(slug="openrouter")
        p.api_key = "sk-test"
        p.is_enabled = True
        p.save()

        result = create_explore_subagent(Mock(), "/workspace/repo/")

        assert isinstance(result, dict)
        assert result["name"] == "explore"
        assert result["description"]
        assert "runnable" in result

    def test_prompt_names_the_working_directory(self):
        """The explore subagent previously had no working-directory info; its prompt must now embed
        it (and drop the stale /repo/ example) so it returns sandbox-absolute paths to the caller."""
        from automation.agent.subagents import _explore_system_prompt

        prompt = _explore_system_prompt("/myrepo/")
        assert "/myrepo/" in prompt
        assert "/repo/src/app/utils.py" not in prompt

    def test_read_only_permissions_deny_all_writes(self):
        """Locks the explore subagent's read-only contract: relaxing this constant would
        silently grant write capability the explore subagent must never have."""
        from deepagents.middleware.filesystem import FilesystemPermission

        from automation.agent.subagents import READ_ONLY_PERMISSIONS

        assert [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")] == READ_ONLY_PERMISSIONS


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
    def mock_runtime_ctx(self, tmp_path):
        ctx = Mock()
        ctx.gitrepo.working_dir = str(tmp_path / "repo")
        return ctx

    async def test_loads_custom_subagent(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(
            _make_subagent_md(name="my-agent", description="Does custom things", body="You do custom things.")
        )

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"
        assert result[0]["description"] == "Does custom things"
        assert "runnable" in result[0]

    async def test_threads_client_and_sandbox_backend_into_middleware(
        self, tmp_path: Path, mock_model, mock_runtime_ctx
    ):
        """The run-scoped client + parent backend are forwarded (positionally, as the last two
        args) into each custom subagent's middleware builder, so a custom subagent's bash tool runs
        through the shared backend rather than raising at runtime. Guards the positional pass-through
        in ``load_custom_subagents``."""
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))

        sentinel_client = Mock()
        sentinel_backend = Mock()
        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        with patch("automation.agent.subagents._build_general_purpose_middleware", return_value=[]) as build_mw:
            result = await load_custom_subagents(
                model=mock_model,
                backend=backend,
                runtime=mock_runtime_ctx,
                sources=["/repo/.agents/subagents"],
                working_directory="/workspace/repo/",
                client=sentinel_client,
                sandbox_backend=sentinel_backend,
            )

        assert len(result) == 1
        build_mw.assert_called_once()
        # client and sandbox_backend are the last two positional args (see load_custom_subagents).
        assert build_mw.call_args.args[-2] is sentinel_client
        assert build_mw.call_args.args[-1] is sentinel_backend

    async def test_loads_multiple_subagents(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "agent-a.md").write_text(_make_subagent_md(name="agent-a", description="Agent A"))
        (subagents_dir / "agent-b.md").write_text(_make_subagent_md(name="agent-b", description="Agent B"))

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        names = {s["name"] for s in result}
        assert names == {"agent-a", "agent-b"}

    async def test_skips_non_md_files(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))
        (subagents_dir / "readme.txt").write_text("Not a subagent")

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"

    async def test_skips_directories(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))
        (subagents_dir / "some-dir").mkdir()

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"

    async def test_skips_missing_name(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad.md").write_text("---\ndescription: no name\n---\nBody here.")

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert len(result) == 0

    async def test_skips_missing_description(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad.md").write_text("---\nname: bad\n---\nBody here.")

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert len(result) == 0

    async def test_skips_empty_body(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "empty.md").write_text("---\nname: empty\ndescription: empty body\n---\n")

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert len(result) == 0

    async def test_skips_no_frontmatter(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "plain.md").write_text("Just some markdown without frontmatter.")

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert len(result) == 0

    async def test_returns_empty_when_no_source_exists(self, mock_model, mock_runtime_ctx):
        backend = Mock()
        backend.als = AsyncMock(side_effect=FileNotFoundError("not found"))

        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert result == []

    async def test_returns_empty_when_source_reports_not_found(self, mock_model, mock_runtime_ctx):
        """An optional source the sandbox now reports as ``not_found`` arrives as a returned
        ``LsResult`` with an error (not a raised exception). The loader must still treat it as absent
        and skip it — an absent optional source is not a failure to surface."""
        from deepagents.backends.protocol import LsResult

        backend = Mock()
        backend.als = AsyncMock(return_value=LsResult(error="Listing '/repo/.agents/subagents': does not exist"))

        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        assert result == []

    @pytest.mark.parametrize("reserved_name", ["general-purpose", "explore", "cr-security"])
    async def test_skips_builtin_name_collision(self, tmp_path: Path, mock_model, mock_runtime_ctx, reserved_name):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / f"{reserved_name}.md").write_text(
            _make_subagent_md(name=reserved_name, description="Trying to override a built-in subagent")
        )
        (subagents_dir / "custom.md").write_text(_make_subagent_md(name="custom", description="Custom agent"))

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        names = {s["name"] for s in result}
        assert reserved_name not in names
        assert "custom" in names

    async def test_skips_invalid_model(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad-model.md").write_text(
            _make_subagent_md(name="bad-model", description="Has invalid model", model="totally-invalid-model")
        )
        (subagents_dir / "good.md").write_text(_make_subagent_md(name="good", description="Good agent"))

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model,
            backend=backend,
            runtime=mock_runtime_ctx,
            sources=["/repo/.agents/subagents"],
            working_directory="/workspace/repo/",
        )

        names = {s["name"] for s in result}
        assert "bad-model" not in names
        assert "good" in names


class TestExplorePermissions:
    def test_sandbox_explore_is_read_only_only(self):
        from automation.agent.subagents import READ_ONLY_PERMISSIONS, _explore_permissions

        assert _explore_permissions(sandbox_enabled=True) == READ_ONLY_PERMISSIONS

    def test_disk_explore_is_read_only_plus_read_fence(self):
        from deepagents.middleware.filesystem import _check_fs_permission

        from automation.agent.subagents import _explore_permissions

        perms = _explore_permissions(sandbox_enabled=False)
        assert _check_fs_permission(perms, "write", "/workspace/repo/foo.py") == "deny"
        assert _check_fs_permission(perms, "read", "/workspace/repo/foo.py") == "allow"
        assert _check_fs_permission(perms, "read", "/workspace/skills/x/SKILL.md") == "allow"
        assert _check_fs_permission(perms, "read", "/workspace") == "deny"
        # offloaded-artifact dirs are readable (eviction read-back) but stay write-denied (read-only agent)
        assert _check_fs_permission(perms, "read", "/workspace/large_tool_results/x") == "allow"
        assert _check_fs_permission(perms, "write", "/workspace/large_tool_results/x") == "deny"


class TestDetectorMiddleware:
    @pytest.fixture
    def mock_backend(self):
        return Mock()

    @pytest.fixture
    def mock_model(self):
        return Mock()

    def test_filesystem_is_read_only(self, mock_model, mock_backend):
        from deepagents.middleware.filesystem import FilesystemMiddleware

        from automation.agent.subagents import READ_ONLY_PERMISSIONS, _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=True, name="cr-correctness")
        fs = next(m for m in middleware if isinstance(m, FilesystemMiddleware))
        assert fs._permissions == READ_ONLY_PERMISSIONS

    def test_includes_sandbox_but_not_git_platform_or_web(self, mock_model, mock_backend):
        from langchain.agents.middleware import TodoListMiddleware

        from automation.agent.middlewares.git_platform import GitPlatformMiddleware
        from automation.agent.middlewares.sandbox import SandboxMiddleware
        from automation.agent.middlewares.web_fetch import WebFetchMiddleware
        from automation.agent.middlewares.web_search import WebSearchMiddleware
        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=True, name="cr-correctness")
        assert any(isinstance(m, SandboxMiddleware) for m in middleware)
        assert not any(isinstance(m, GitPlatformMiddleware) for m in middleware)
        assert not any(isinstance(m, WebSearchMiddleware) for m in middleware)
        assert not any(isinstance(m, WebFetchMiddleware) for m in middleware)
        assert not any(isinstance(m, TodoListMiddleware) for m in middleware)

    def test_excludes_sandbox_when_disabled(self, mock_model, mock_backend):
        from automation.agent.middlewares.sandbox import SandboxMiddleware
        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=False, name="cr-correctness")
        assert not any(isinstance(m, SandboxMiddleware) for m in middleware)

    def test_threads_client_and_sandbox_backend_into_sandbox_middleware(self, mock_model, mock_backend):
        # Detectors reuse the run's bound client/sandbox_backend so their bash runs in the parent's
        # session (close_session=False). A dropped/flipped kwarg would make every detector's bash
        # raise "SandboxFileBackend is not bound to a sandbox session" at runtime — guard the
        # threading exactly like the general-purpose builder's equivalent test does.
        from automation.agent.subagents import _build_detector_middleware

        sentinel_client = Mock()
        sentinel_backend = Mock()
        middleware = _build_detector_middleware(
            mock_model,
            mock_backend,
            sandbox_enabled=True,
            client=sentinel_client,
            sandbox_backend=sentinel_backend,
            name="cr-correctness",
        )
        sandbox_mw = next(m for m in middleware if isinstance(m, SandboxMiddleware))
        assert sandbox_mw._client is sentinel_client
        assert sandbox_mw._sandbox_backend is sentinel_backend
        assert sandbox_mw.close_session is False

    def test_includes_deferred_output_middleware(self, mock_model, mock_backend):
        from automation.agent.constants import SUBAGENT_OUTPUT_PATH
        from automation.agent.middlewares.deferred_output import DeferredOutputMiddleware
        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=True, name="cr-correctness")
        deferred = [m for m in middleware if isinstance(m, DeferredOutputMiddleware)]
        assert len(deferred) == 1
        assert deferred[0]._name == "cr-correctness"
        assert deferred[0]._output_dir == SUBAGENT_OUTPUT_PATH

    def test_deferred_output_runs_before_sandbox_teardown(self, mock_model, mock_backend):
        # The detector defers its findings file in DeferredOutputMiddleware.aafter_agent, which must
        # complete while the shared sandbox session is still alive. after_agent hooks fire in reverse
        # append order, so DeferredOutputMiddleware must be appended AFTER SandboxMiddleware to run
        # first on the way out. Lock both halves of that guard: the relative order, and that the
        # detector's SandboxMiddleware never closes the (parent-owned) session itself.
        from automation.agent.middlewares.deferred_output import DeferredOutputMiddleware
        from automation.agent.middlewares.sandbox import SandboxMiddleware
        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=True, name="cr-correctness")
        sandbox_idx = next(i for i, m in enumerate(middleware) if isinstance(m, SandboxMiddleware))
        deferred_idx = next(i for i, m in enumerate(middleware) if isinstance(m, DeferredOutputMiddleware))
        assert deferred_idx > sandbox_idx, "DeferredOutputMiddleware must be appended after SandboxMiddleware"
        assert middleware[sandbox_idx].close_session is False

    def test_detector_middleware_has_enforcer_inside_loop_breaker_and_deferred_last(self, mock_model, mock_backend):
        from automation.agent.middlewares.deferred_output import DeferredOutputMiddleware
        from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
        from automation.agent.middlewares.submit_findings import SubmitFindingsEnforcerMiddleware
        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=False, name="cr-correctness")

        types = [type(m) for m in middleware]
        assert SubmitFindingsEnforcerMiddleware in types
        # The enforcer must nest INSIDE the breaker: a LoopBreaker terminal response is
        # tool-call-free and unsubmitted, and an outer enforcer would nudge-retry it back to life.
        assert types.index(LoopBreakerMiddleware) < types.index(SubmitFindingsEnforcerMiddleware)
        assert isinstance(middleware[-1], DeferredOutputMiddleware)

    def test_shared_subagent_middleware_includes_heartbeat_step_budget(self, mock_model, mock_backend):
        from automation.agent.middlewares.loop_breaker import LoopBreakerMiddleware
        from automation.agent.middlewares.step_budget import StepBudgetMiddleware
        from automation.agent.subagents import SUBAGENT_HEARTBEAT_EVERY_CALLS, _shared_subagent_middleware

        stack = _shared_subagent_middleware(mock_model, mock_backend)

        budgets = [m for m in stack if isinstance(m, StepBudgetMiddleware)]
        assert len(budgets) == 1
        assert budgets[0].heartbeat_every_calls == SUBAGENT_HEARTBEAT_EVERY_CALLS
        # The breaker stays outer: its terminal response must short-circuit past the heartbeat.
        types = [type(m) for m in stack]
        assert types.index(LoopBreakerMiddleware) < types.index(StepBudgetMiddleware)


class TestShippedDetectorCharters:
    """Lock the five detector charter files that ship inside the code-review skill."""

    def test_all_five_detectors_present_and_wellformed(self):
        from automation.agent.subagents import (
            CODE_REVIEW_AGENTS_PATH,
            CODE_REVIEW_DETECTOR_NAMES,
            _parse_subagent_frontmatter,
        )

        md_files = sorted(CODE_REVIEW_AGENTS_PATH.glob("*.md"))
        names = set()
        for md in md_files:
            parsed = _parse_subagent_frontmatter(
                md.read_text(encoding="utf-8"), str(md), permitted_reserved_names=frozenset(CODE_REVIEW_DETECTOR_NAMES)
            )
            assert parsed is not None, f"{md.name} failed frontmatter parse"
            frontmatter, body = parsed
            assert frontmatter["description"].strip()
            assert body.strip()
            names.add(frontmatter["name"])
        assert names == set(CODE_REVIEW_DETECTOR_NAMES)

    def test_shared_preamble_carries_read_only_bash_directive(self):
        # The detector sandbox is a full bash shell — no read-only mount and no per-subagent
        # command policy (SandboxMiddleware.__init__ takes no policy arg; _check_command_policy
        # reads only global settings + repo config). So read-only is enforced at the prompt layer:
        # the SHARED_DETECTOR_PREAMBLE prepended to every charter (load_builtin_code_review_detectors)
        # must carry the read-only bash directive. Locked so an edit can't silently drop it and let a
        # detector mutate the shared workspace via bash.
        from automation.agent.subagents import SHARED_DETECTOR_PREAMBLE

        body = SHARED_DETECTOR_PREAMBLE.lower()
        assert "read-only" in body, "shared preamble is missing the read-only directive"
        assert "sed -i" in body, "shared preamble is missing the no-mutation command guidance"

    def test_agents_dir_holds_exactly_the_five_cr_charters(self):
        # review-workflow.md's inline-detection fallback tells the parent to read
        # `agents/cr-*.md`. Lock that this literal glob resolves to exactly the five detector
        # charters, so renaming the dir or a file (silently breaking that reference) is caught.
        from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH, CODE_REVIEW_DETECTOR_NAMES

        stems = {p.stem for p in CODE_REVIEW_AGENTS_PATH.glob("cr-*.md")}
        assert stems == set(CODE_REVIEW_DETECTOR_NAMES)

    def test_principle_citations_resolve_to_existing_sections(self):
        # The charters cite principles.md sections as ``§N``; those numbers are coupled to the
        # ``## N.`` headings by convention only. Reordering/inserting a section in principles.md
        # would silently invalidate the citations with no other test failing — so guard the
        # coupling here: every cited ``§N`` must resolve to a ``## N.`` heading that exists.
        import re

        from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH

        principles = (CODE_REVIEW_AGENTS_PATH.parent / "references" / "principles.md").read_text(encoding="utf-8")
        existing = {int(n) for n in re.findall(r"^##\s+(\d+)\.", principles, re.MULTILINE)}
        assert existing, "no numbered sections found in principles.md"

        for md in sorted(CODE_REVIEW_AGENTS_PATH.glob("*.md")):
            cited = {int(n) for n in re.findall(r"§(\d+)", md.read_text(encoding="utf-8"))}
            missing = cited - existing
            assert not missing, f"{md.name} cites principles.md sections that don't exist: {sorted(missing)}"


class TestBuiltinCodeReviewDetectors:
    @pytest.fixture
    def mock_backend(self):
        return Mock()

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        ctx = Mock()
        ctx.gitrepo.working_dir = str(repo_dir)
        return ctx

    def test_findings_schema_wraps_finding_schema(self):
        from automation.agent.subagents import _load_detector_findings_schema

        rf = _load_detector_findings_schema()
        assert rf["type"] == "object"
        assert rf["required"] == ["findings"]
        assert rf["properties"]["findings"]["type"] == "array"
        item = rf["properties"]["findings"]["items"]
        assert item["properties"]["detector"]["enum"] == [
            "correctness",
            "security",
            "performance",
            "structure",
            "custom-rules",
        ]

    def test_loads_detectors_from_dir(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text(
            _make_subagent_md(name="cr-correctness", description="Correctness detector", body="Find correctness bugs.")
        )
        (agents_dir / "cr-security.md").write_text(
            _make_subagent_md(name="cr-security", description="Security detector", body="Find security bugs.")
        )

        result = load_builtin_code_review_detectors(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            working_directory="/workspace/repo/",
            sandbox_enabled=False,
            agents_dir=agents_dir,
        )

        names = {s["name"] for s in result}
        assert names == {"cr-correctness", "cr-security"}
        assert all("runnable" in s for s in result)

    def test_returns_empty_when_dir_missing(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        from automation.agent.subagents import load_builtin_code_review_detectors

        result = load_builtin_code_review_detectors(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            working_directory="/workspace/repo/",
            sandbox_enabled=False,
            agents_dir=tmp_path / "missing",
        )
        assert result == []

    def test_skips_invalid_model_detector(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx, caplog):
        # A ValueError from get_model is a charter config typo (unknown/empty model spec). The
        # narrowed handler logs it at WARNING as "invalid model" — NOT via logger.exception — which
        # is the other half of the env-vs-typo split asserted in the environmental-error test.
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text(
            _make_subagent_md(name="cr-correctness", description="Bad model", body="x", model="some:model")
        )
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        with (
            caplog.at_level(logging.WARNING, logger="daiv.agent"),
            patch("automation.agent.subagents.BaseAgent.get_model", side_effect=ValueError("unknown provider")),
        ):
            result = load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
            )
        assert {s["name"] for s in result} == {"cr-security"}

        invalid_records = [r for r in caplog.records if "invalid model" in r.message]
        assert len(invalid_records) == 1
        assert invalid_records[0].levelno == logging.WARNING
        assert invalid_records[0].exc_info is None  # logger.warning, not logger.exception
        assert not any("failed to initialize" in r.message for r in caplog.records)

    def test_real_shipped_charters_load_all_five(self, mock_model, mock_backend, mock_runtime_ctx):
        from automation.agent.subagents import CODE_REVIEW_DETECTOR_NAMES, load_builtin_code_review_detectors

        result = load_builtin_code_review_detectors(
            mock_model, mock_backend, mock_runtime_ctx, working_directory="/workspace/repo/", sandbox_enabled=False
        )
        assert {s["name"] for s in result} == set(CODE_REVIEW_DETECTOR_NAMES)

    def test_skips_unreadable_charter_loads_rest(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        # An unreadable charter must degrade the review to the detectors that loaded, never abort
        # the synchronous graph build. A directory named ``*.md`` is matched by the glob but raises
        # IsADirectoryError (an OSError subclass) on read_text — exercising the except OSError skip.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").mkdir()  # unreadable: a directory, not a file
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        result = load_builtin_code_review_detectors(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            working_directory="/workspace/repo/",
            sandbox_enabled=False,
            agents_dir=agents_dir,
        )
        assert {s["name"] for s in result} == {"cr-security"}

    def test_skips_malformed_charter_loads_rest(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        # A charter with no parseable frontmatter (``_parse_subagent_frontmatter`` -> None) is skipped
        # while siblings load. The detector loader has its own ``parsed is None: continue`` branch.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text("no frontmatter here, just prose")
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        result = load_builtin_code_review_detectors(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            working_directory="/workspace/repo/",
            sandbox_enabled=False,
            agents_dir=agents_dir,
        )
        assert {s["name"] for s in result} == {"cr-security"}

    def test_environmental_model_error_skips_detector_not_aborts(
        self, tmp_path, mock_model, mock_backend, mock_runtime_ctx, caplog
    ):
        # A non-ValueError from get_model (disabled provider, missing key, SDK init failure) is an
        # environment problem, not a charter typo: skip just that detector and load the rest, rather
        # than aborting the whole agent build. The narrowed handler logs it via logger.exception
        # (ERROR + traceback) with a "failed to initialize" message — NOT mislabeled "invalid model" —
        # so an env failure is distinguishable from a config typo. Asserting the level/message/exc_info
        # pins the split: collapsing both handlers into one warning("invalid model") would fail here.
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text(
            _make_subagent_md(name="cr-correctness", description="Env-broken model", body="x", model="some:model")
        )
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        with (
            caplog.at_level(logging.WARNING, logger="daiv.agent"),
            patch("automation.agent.subagents.BaseAgent.get_model", side_effect=RuntimeError("provider disabled")),
        ):
            result = load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
            )
        assert {s["name"] for s in result} == {"cr-security"}

        init_records = [r for r in caplog.records if "failed to initialize" in r.message]
        assert len(init_records) == 1
        assert init_records[0].levelno == logging.ERROR
        assert init_records[0].exc_info is not None  # logger.exception captured the traceback
        assert not any("invalid model" in r.message for r in caplog.records)  # not mislabeled

    def test_logs_error_with_missing_names_when_some_fail(
        self, tmp_path, mock_model, mock_backend, mock_runtime_ctx, caplog
    ):
        # Ground-truth reconciliation: a charter present-but-not-compiled is a silently-absent
        # review dimension, so the shortfall is surfaced at ERROR with the failed file stems.
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text("no frontmatter")  # fails to parse
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            result = load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
            )

        assert {s["name"] for s in result} == {"cr-security"}
        assert any("cr-correctness" in r.message and "loaded 1/2" in r.message for r in caplog.records)

    def test_no_error_logged_when_all_load(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx, caplog):
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
            )
        assert not [r for r in caplog.records if "failed to load" in r.message]

    def test_detectors_compiled_with_submit_findings_tool(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        # Detectors must NOT use forced structured output: response_format makes langchain force
        # tool_choice="any" on every model call, so the model can never think in text nor stop —
        # a weak model pattern-locks into token-burning read loops (the 68M-token runaway).
        # Findings flow through the submit_findings tool instead; dropping the tool (or
        # reintroducing response_format=) would silently break the review merge.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text(
            _make_subagent_md(name="cr-correctness", description="Correctness detector", body="Find correctness bugs.")
        )
        (agents_dir / "cr-security.md").write_text(
            _make_subagent_md(name="cr-security", description="Security detector", body="Find security bugs.")
        )

        with patch("automation.agent.subagents.create_agent") as mock_create:
            mock_create.return_value = Mock()
            load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
            )

        assert mock_create.call_count == 2
        assert all(call.kwargs["response_format"] is None for call in mock_create.call_args_list)
        for call in mock_create.call_args_list:
            assert [tool.name for tool in call.kwargs["tools"]] == ["submit_findings"]

    def test_returns_empty_when_schema_missing(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx, caplog):
        # A missing finding schema must degrade code-review to no detectors, NOT abort the whole
        # agent build (this loader runs inside create_daiv_agent on every run). Mirrors the
        # missing-dir guard: return [] and surface the cause at ERROR.
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            result = load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
                schema_path=tmp_path / "missing.json",
            )
        assert result == []
        assert any("missing or invalid" in r.message for r in caplog.records)

    def test_returns_empty_when_schema_corrupt(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        # A malformed schema (JSONDecodeError) degrades the same way as a missing one.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))
        bad_schema = tmp_path / "bad.json"
        bad_schema.write_text("{ not valid json")

        result = load_builtin_code_review_detectors(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            working_directory="/workspace/repo/",
            sandbox_enabled=False,
            agents_dir=agents_dir,
            schema_path=bad_schema,
        )
        assert result == []

    def test_detector_model_override_used_when_valid(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        # A charter `model:` override must actually replace the default for that detector. The two
        # failure branches (config typo / env error) are tested; pin the happy path so a regression
        # that ignored the override (always compiling with the parent model) would be caught.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-security.md").write_text(
            _make_subagent_md(name="cr-security", description="Good", body="y", model="some:override")
        )
        override_model = Mock()

        with (
            patch("automation.agent.subagents.BaseAgent.get_model", return_value=override_model) as get_model,
            patch("automation.agent.subagents.create_agent") as mock_create,
        ):
            mock_create.return_value = Mock()
            load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
            )

        get_model.assert_called_once_with(model="some:override")
        assert mock_create.call_args.kwargs["model"] is override_model
