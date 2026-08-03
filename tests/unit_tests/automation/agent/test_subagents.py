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
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import ModelFallbackMiddleware

from automation.agent.middlewares.file_system import DAIVFilesystemMiddleware
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
        return Mock(spec=BackendProtocol)

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
        middleware = _build_detector_middleware(mock_model, mock_backend)
        breakers = [m for m in middleware if isinstance(m, LoopBreakerMiddleware)]
        assert len(breakers) == 1
        assert breakers[0].terminal == "error"

    def test_detector_stack_uses_daiv_filesystem_middleware(self, mock_model, mock_backend):
        """The grep output-mode label lives on DAIV's filesystem subclass, so wiring the plain
        upstream middleware would hand the detectors grep tools with no label.
        """
        middleware = _build_detector_middleware(mock_model, mock_backend)
        fs = [m for m in middleware if isinstance(m, FilesystemMiddleware)]
        assert fs and all(isinstance(m, DAIVFilesystemMiddleware) for m in fs)

    def test_general_purpose_stack_uses_daiv_filesystem_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=False,
            web_fetch_enabled=False,
        )
        fs = [m for m in middleware if isinstance(m, FilesystemMiddleware)]
        assert fs and all(isinstance(m, DAIVFilesystemMiddleware) for m in fs)

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
        return Mock(spec=BackendProtocol)

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
        return Mock(spec=BackendProtocol)

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

        result = create_explore_subagent(Mock(spec=BackendProtocol), "/workspace/repo/")

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
        return Mock(spec=BackendProtocol)

    @pytest.fixture
    def mock_model(self):
        return Mock()

    def test_filesystem_is_read_only(self, mock_model, mock_backend):
        from automation.agent.middlewares.file_system import READ_ONLY_FS_TOOLS
        from automation.agent.subagents import READ_ONLY_PERMISSIONS, _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend)
        fs = next(m for m in middleware if isinstance(m, FilesystemMiddleware))
        assert fs._permissions == READ_ONLY_PERMISSIONS
        # No sandbox means no shell, so the filesystem layer is now the *whole* write fence rather
        # than one half of it: a detector must not even be handed a write tool to be denied.
        exposed = {tool.name for tool in fs.tools}
        assert exposed == set(READ_ONLY_FS_TOOLS)
        assert not exposed & {"write_file", "edit_file"}

    def test_excludes_sandbox_git_platform_web_and_todos(self, mock_model, mock_backend):
        # Detectors review a pre-computed diff and read source for context, so the stack is
        # filesystem-only. The sandbox was removed along with the read-only-bash prompt contract:
        # no shell is reachable, so there is nothing left for a charter to promise not to mutate.
        from langchain.agents.middleware import TodoListMiddleware

        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend)
        assert not any(isinstance(m, SandboxMiddleware) for m in middleware)
        assert not any(isinstance(m, GitPlatformMiddleware) for m in middleware)
        assert not any(isinstance(m, WebSearchMiddleware) for m in middleware)
        assert not any(isinstance(m, WebFetchMiddleware) for m in middleware)
        assert not any(isinstance(m, TodoListMiddleware) for m in middleware)

    def test_no_deferred_output_middleware(self, mock_model, mock_backend):
        # Detectors return their prose report inline as the task result; nothing may divert the
        # final message to a file. Guards against reintroducing DeferredOutputMiddleware (removed
        # with the structured-findings pipeline).
        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend)
        assert not any(type(m).__name__ == "DeferredOutputMiddleware" for m in middleware)


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
            parsed = _parse_subagent_frontmatter(md.read_text(encoding="utf-8"), str(md))
            assert parsed is not None, f"{md.name} failed frontmatter parse"
            frontmatter, body = parsed
            assert frontmatter["description"].strip()
            assert body.strip()
            names.add(frontmatter["name"])
        assert names == set(CODE_REVIEW_DETECTOR_NAMES)

    def test_agents_dir_holds_exactly_the_five_cr_charters(self):
        # The loader globs `*.md`, not `cr-*.md`; this test additionally locks that the `cr-`
        # prefix convention holds, so a renamed or added charter — silently dropping a dimension or
        # inventing one — is caught.
        from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH, CODE_REVIEW_DETECTOR_NAMES

        stems = {p.stem for p in CODE_REVIEW_AGENTS_PATH.glob("cr-*.md")}
        assert stems == set(CODE_REVIEW_DETECTOR_NAMES)

    def test_charters_carry_precision_gate_and_report_contract(self):
        # The charters are self-contained, so the precision blocks live inside each file: the >=80
        # gate, the severity subset for that dimension, the read-only and scope rules, and the report
        # sentinels. Each charter now owns its own dimension-specific wording, so this is the only
        # test standing between a charter edit and a detector whose output the orchestrator cannot
        # classify.
        from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH

        expected_severities = {
            "cr-correctness.md": {"Critical", "Important"},
            "cr-security.md": {"Critical", "Important"},
            "cr-performance.md": {"Critical", "Important"},
            "cr-structure.md": {"Important", "Suggestion"},
            "cr-custom-rules.md": {"Critical", "Important", "Suggestion"},
        }

        for md in sorted(CODE_REVIEW_AGENTS_PATH.glob("cr-*.md")):
            body = md.read_text(encoding="utf-8")
            assert "Report only confidence 80 or higher." in body, (
                f"{md.name} lost the >=80 reporting threshold sentence"
            )
            assert "## Severity" in body, f"{md.name} lost the severity rubric"

            present = {label for label in ("Critical", "Important", "Suggestion") if f"- **{label}**" in body}
            assert present == expected_severities[md.name], (
                f"{md.name} severity grades {present} != expected {expected_severities[md.name]}"
            )
            # A question is an entry shape, not a fourth severity grade: the orchestrator sections
            # questions separately and exempts them from the confidence gate. `- **Question:**` is a
            # legitimate field *inside* a `### Question:` entry, so the grade check is the severity
            # subset above plus the graded-label assertion in the shared-lines test — not the absence
            # of the string.
            assert "### Question: <one-line subject>" in body, f"{md.name} lost the question entry format"
            assert "**Verify:**" not in body, f"{md.name} still carries the removed Verify field"
            assert "- **Ask:**" not in body, f"{md.name} still carries the removed Question Ask variant"

            # Prompt-layer read-only contract. With the sandbox gone this is belt to the filesystem
            # layer's braces (READ_ONLY_FS_TOOLS), and it is what keeps the charter honest if a shell
            # is ever wired back in.
            assert "Use only filesystem read and search tools. Do not use Bash" in body, (
                f"{md.name} lost the read-only tool contract"
            )
            # Widening scope is what turns a five-way fan-out over one diff into five reviews of
            # different changes, which no amount of deduplication can reconcile. The full sentence is
            # dimension-specific, so only the invariant clause is asserted.
            assert "Do not reconstruct the diff" in body, f"{md.name} lost the do-not-widen-scope rule"
            assert "Return only the report." in body, f"{md.name} lost the report-only instruction"
            assert "No findings." in body, f"{md.name} lost the no-findings sentinel"
            assert "`ERROR: could not read the complete canonical diff.`" in body, (
                f"{md.name} lost the unreadable-diff ERROR contract"
            )
            assert "**Confidence:**" in body, f"{md.name} lost the confidence field in the report format"
            assert "- **Location:**" in body, f"{md.name} lost the location anchor field"

        custom = (CODE_REVIEW_AGENTS_PATH / "cr-custom-rules.md").read_text(encoding="utf-8")
        assert "**Rule:**" in custom, "cr-custom-rules lost the rule-citation field"
        assert "ERROR: could not read rule source" in custom, (
            "cr-custom-rules lost the unreadable-rule-source ERROR contract"
        )

    def test_charters_share_the_lines_the_aggregator_keys_on(self):
        # Every charter section is dimension-specific prose now, so nothing is byte-identical
        # between them — but the aggregator still reads five reports on one scale. What has to stay
        # common is the machine-readable surface: the 0-100/>=80 gate sentence, the entry headers,
        # the fields the orchestrator strips or sections on, and the two terminal sentinels it
        # classifies results by. Asserted as an intersection over all five so a charter rewrite that
        # rewords any of them for one dimension alone fails here instead of silently producing a
        # report the orchestrator scores as a crash.
        from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH

        charters = {
            md.name: {line.strip() for line in md.read_text(encoding="utf-8").splitlines() if line.strip()}
            for md in sorted(CODE_REVIEW_AGENTS_PATH.glob("cr-*.md"))
        }
        assert len(charters) == 5
        shared = set.intersection(*charters.values())

        # The >=80 gate sentence is shared, but its step number is not: cr-custom-rules
        # carries an extra rule-source protocol step, so the scoring line is step 8 there
        # and step 7 in the other four. Assert the sentence, not the leading step number.
        gate = "Score candidates internally from 0–100. Report only confidence 80 or higher."
        for name, lines in charters.items():
            assert any(line.endswith(gate) for line in lines), f"{name} lost the >=80 reporting threshold gate sentence"

        for line in (
            # Section headings the charters are structured by.
            "## Scope and operating constraints",
            "## Severity",
            "## Output",
            # Entry headers SKILL.md step 5 recognises findings and questions by.
            "### <Severity>: <one-line title>",
            "### Question: <one-line subject>",
            # Fields the orchestrator strips (Confidence) or anchors on (Location).
            "- **Confidence:** <80–100>",
            "- **Location:** `path/to/file.py:42` or `path/to/file.py:42 (deleted)`",
            "- **Location:** `path/to/file.py:42`",
            "Omit the location when no changed line applies to the question.",
            # The two terminal sentinels step 5 classifies a detector's result by, each with the
            # exact-response preamble that makes the model emit it alone.
            "2. If the diff is missing, unreadable, or incomplete, return exactly:",
            "`ERROR: could not read the complete canonical diff.`",
            "If nothing qualifies, your entire final response must be exactly:",
            "`No findings.`",
        ):
            assert line in shared, f"contract line is no longer shared by all five charters: {line!r}"

        # No charter may invent a grade outside the three the orchestrator can order: its
        # keep-the-higher pass on a deduplicated finding has no place to put a fourth label, and
        # SKILL.md's report template has no section to print it under.
        import re

        for md in sorted(CODE_REVIEW_AGENTS_PATH.glob("cr-*.md")):
            severity = re.search(r"^## Severity\n(.*?)(?=^## |\Z)", md.read_text(encoding="utf-8"), re.S | re.M)
            assert severity, f"{md.name} lost its '## Severity' section"
            graded = {line.split("**")[1] for line in severity.group(1).splitlines() if line.startswith("- **")}
            assert graded <= {"Critical", "Important", "Suggestion"}, (
                f"{md.name} grades severities the orchestrator cannot order: {graded}"
            )


class TestBuiltinCodeReviewDetectors:
    @pytest.fixture
    def mock_backend(self):
        return Mock(spec=BackendProtocol)

    @pytest.fixture
    def mock_model(self):
        return Mock()

    def test_loads_detectors_from_dir(self, tmp_path, mock_model, mock_backend):
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
            mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=agents_dir
        )

        names = {s["name"] for s in result}
        assert names == {"cr-correctness", "cr-security"}
        assert all("runnable" in s for s in result)

    def test_returns_empty_when_dir_missing(self, tmp_path, mock_model, mock_backend):
        from automation.agent.subagents import load_builtin_code_review_detectors

        result = load_builtin_code_review_detectors(
            mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=tmp_path / "missing"
        )
        assert result == []

    def test_skips_invalid_model_detector(self, tmp_path, mock_model, mock_backend, caplog):
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
                mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=agents_dir
            )
        assert {s["name"] for s in result} == {"cr-security"}

        invalid_records = [r for r in caplog.records if "invalid model" in r.message]
        assert len(invalid_records) == 1
        assert invalid_records[0].levelno == logging.WARNING
        assert invalid_records[0].exc_info is None  # logger.warning, not logger.exception
        assert not any("failed to initialize" in r.message for r in caplog.records)

    def test_real_shipped_charters_load_all_five(self, mock_model, mock_backend):
        from automation.agent.subagents import CODE_REVIEW_DETECTOR_NAMES, load_builtin_code_review_detectors

        result = load_builtin_code_review_detectors(mock_model, mock_backend, working_directory="/workspace/repo/")
        assert {s["name"] for s in result} == set(CODE_REVIEW_DETECTOR_NAMES)

    def test_skips_unreadable_charter_loads_rest(self, tmp_path, mock_model, mock_backend):
        # An unreadable charter must degrade the review to the detectors that loaded, never abort
        # the synchronous graph build. A directory named ``*.md`` is matched by the glob but raises
        # IsADirectoryError (an OSError subclass) on read_text — exercising the except OSError skip.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").mkdir()  # unreadable: a directory, not a file
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        result = load_builtin_code_review_detectors(
            mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=agents_dir
        )
        assert {s["name"] for s in result} == {"cr-security"}

    def test_skips_malformed_charter_loads_rest(self, tmp_path, mock_model, mock_backend):
        # A charter with no parseable frontmatter (``_parse_subagent_frontmatter`` -> None) is skipped
        # while siblings load. The detector loader has its own ``parsed is None: continue`` branch.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text("no frontmatter here, just prose")
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        result = load_builtin_code_review_detectors(
            mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=agents_dir
        )
        assert {s["name"] for s in result} == {"cr-security"}

    def test_environmental_model_error_skips_detector_not_aborts(self, tmp_path, mock_model, mock_backend, caplog):
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
                mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=agents_dir
            )
        assert {s["name"] for s in result} == {"cr-security"}

        init_records = [r for r in caplog.records if "failed to initialize" in r.message]
        assert len(init_records) == 1
        assert init_records[0].levelno == logging.ERROR
        assert init_records[0].exc_info is not None  # logger.exception captured the traceback
        assert not any("invalid model" in r.message for r in caplog.records)  # not mislabeled

    def test_logs_error_with_missing_names_when_some_fail(self, tmp_path, mock_model, mock_backend, caplog):
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
                working_directory="/workspace/repo/",
                agents_dir=agents_dir,
                expected_names=("cr-correctness", "cr-security"),
            )

        assert {s["name"] for s in result} == {"cr-security"}
        assert any("failed=cr-correctness" in r.message and "loaded 1/2 expected" in r.message for r in caplog.records)

    def test_logs_error_when_an_expected_charter_is_absent(self, tmp_path, mock_model, mock_backend, caplog):
        # The likeliest deploy shortfall is a charter that isn't there at all — renamed, deleted, or
        # not packaged. It never enters the compile loop, so reconciling against the *directory* saw
        # nothing wrong and happily reported "loaded 1/1". Reconcile against the expected roster.
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            result = load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                working_directory="/workspace/repo/",
                agents_dir=agents_dir,
                expected_names=("cr-correctness", "cr-security"),
            )

        assert {s["name"] for s in result} == {"cr-security"}
        assert any("absent=cr-correctness" in r.message and "loaded 1/2 expected" in r.message for r in caplog.records)

    def test_logs_error_when_agents_dir_is_missing(self, tmp_path, mock_model, mock_backend, caplog):
        # A missing dir is the whole capability gone (every dimension uncovered), so it logs louder
        # than one bad charter, not quieter.
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            result = load_builtin_code_review_detectors(
                mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=tmp_path / "does-not-exist"
            )

        assert result == []
        assert any(r.levelno == logging.ERROR and "not found" in r.message for r in caplog.records)

    def test_no_error_logged_when_all_load(self, tmp_path, mock_model, mock_backend, caplog):
        import logging

        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-security.md").write_text(_make_subagent_md(name="cr-security", description="Good", body="y"))

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                working_directory="/workspace/repo/",
                agents_dir=agents_dir,
                expected_names=("cr-security",),
            )
        assert not [r for r in caplog.records if "unavailable" in r.message]

    def test_detectors_compiled_without_response_format(self, tmp_path, mock_model, mock_backend):
        # Detectors are prose reporters: a response_format would force tool_choice="any", remove
        # the natural text stop, and re-open the runaway-loop failure mode the structured pipeline
        # had. Assert no compiled detector carries one.
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
                mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=agents_dir
            )

        assert mock_create.call_count == 2
        assert all(call.kwargs.get("response_format") is None for call in mock_create.call_args_list)

    async def test_detector_crash_becomes_an_error_report_instead_of_aborting_the_run(self):
        # deepagents' `task` tool awaits the subagent's ainvoke with no error handling, and
        # create_agent builds its ToolNode without handle_tool_errors — so anything a detector
        # raises would propagate out of the ToolNode and kill the parent review, discarding every
        # sibling detector's completed work. The guard converts it into the same `ERROR:` final
        # message the LoopBreaker produces, which SKILL.md Step 5 classifies as an uncovered
        # dimension. Without this, the skill's "continue with the rest" contract is unenforceable.
        from langgraph.errors import GraphRecursionError

        from automation.agent.subagents import _guard_subagent_crash

        inner = Mock()
        inner.ainvoke = AsyncMock(side_effect=GraphRecursionError("recursion limit reached"))

        result = await _guard_subagent_crash(inner, "cr-correctness").ainvoke({"messages": []})

        assert result["messages"][-1].content.startswith("ERROR:")
        assert "cr-correctness" in result["messages"][-1].content

    async def test_crash_guard_does_not_swallow_cancellation(self):
        # Cancellation must stay fatal: swallowing CancelledError would turn a cancelled review into
        # a report of five failed detectors and keep the run alive past its own teardown.
        import asyncio

        from automation.agent.subagents import _guard_subagent_crash

        inner = Mock()
        inner.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await _guard_subagent_crash(inner, "cr-security").ainvoke({"messages": []})

    def test_detector_model_override_used_when_valid(self, tmp_path, mock_model, mock_backend):
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
                mock_model, mock_backend, working_directory="/workspace/repo/", agents_dir=agents_dir
            )

        get_model.assert_called_once_with(model="some:override")
        assert mock_create.call_args.kwargs["model"] is override_model
