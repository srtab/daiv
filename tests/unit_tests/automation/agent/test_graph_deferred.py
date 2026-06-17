from unittest.mock import AsyncMock, MagicMock, patch


def _common_patches():
    """Patches that disable side effects unrelated to the deferred-tools wiring."""
    return [
        patch("automation.agent.graph.build_disk_workspace_backend"),
        patch("automation.agent.graph.DAIVCompositeBackend"),
        patch("automation.agent.graph.create_general_purpose_subagent"),
        patch("automation.agent.graph.create_explore_subagent"),
        patch("automation.agent.graph.load_custom_subagents", AsyncMock(return_value=[])),
        patch("automation.agent.graph.create_deep_agent"),
        patch("automation.agent.graph.MCPToolkit"),
        # The defer-or-bind decision now reads deferred_settings through the shared helpers in
        # middlewares.deferred_tools (used by both the main agent and subagents), so patch it there.
        patch("automation.agent.middlewares.deferred_tools.deferred_settings"),
        patch("automation.agent.graph.BaseAgent"),
        patch("automation.agent.graph.site_settings"),
        # Middleware constructors that validate input — replaced with no-op mocks
        patch("automation.agent.graph.SkillsMiddleware"),
        patch("automation.agent.graph.AnthropicPromptCachingMiddleware"),
        patch("automation.agent.graph.GitMiddleware"),
        patch("automation.agent.graph.GitPlatformMiddleware"),
        patch("automation.agent.graph.ToolCallLoggingMiddleware"),
    ]


class TestCreateDaivAgentDeferredFlag:
    async def _run(self, *, flag_on: bool, tools: list | None = None):
        from langchain_core.tools import StructuredTool

        from automation.agent.graph import create_daiv_agent

        patches = _common_patches()
        managers = [p.start() for p in patches]
        try:
            (
                mock_fs_backend,
                mock_composite_backend,
                mock_create_gp,
                mock_create_explore,
                mock_load_custom,
                mock_create_deep_agent,
                mock_toolkit,
                mock_deferred_settings,
                mock_base_agent,
                mock_site_settings,
                *_,
            ) = managers

            mock_deferred_settings.ENABLED = flag_on
            mock_deferred_settings.TOP_K_DEFAULT = 5
            mock_deferred_settings.TOP_K_MAX = 10

            mcp_tool = StructuredTool.from_function(func=lambda **k: "x", name="t1", description="d")
            # tools=None => default single-tool list; pass tools=[] to exercise the empty-toolset path.
            mock_toolkit.get_tools = AsyncMock(return_value=[mcp_tool] if tools is None else tools)

            mock_base_agent.get_model.return_value = MagicMock()

            mock_site_settings.agent_recursion_limit = 50
            mock_site_settings.agent_model_name = "claude"
            mock_site_settings.agent_fallback_model_name = "claude"
            mock_site_settings.agent_thinking_level = None
            mock_site_settings.web_fetch_enabled = False
            mock_site_settings.web_search_enabled = False

            mock_create_deep_agent.return_value.with_config.return_value = MagicMock()

            ctx = MagicMock()
            ctx.gitrepo.working_dir = "/repo"
            ctx.sandbox.enabled = False
            ctx.config.context_file_name = "AGENTS.md"
            ctx.git_platform = MagicMock()

            await create_daiv_agent(ctx=ctx, auto_commit_changes=False)
            return (
                mock_create_deep_agent,
                mock_toolkit,
                mcp_tool,
                mock_create_gp,
                mock_load_custom,
                mock_create_explore,
            )
        finally:
            for p in patches:
                p.stop()

    async def test_flag_off_passes_eager_tools(self):
        mock_create_deep_agent, mock_toolkit, mcp_tool, *_ = await self._run(flag_on=False)

        mock_toolkit.get_tools.assert_awaited()
        kwargs = mock_create_deep_agent.call_args.kwargs
        assert kwargs["tools"] == [mcp_tool]
        middleware_types = [type(m).__name__ for m in kwargs["middleware"]]
        assert "DeferredToolsMiddleware" not in middleware_types

    async def test_flag_on_passes_empty_tools_and_adds_middleware(self):
        mock_create_deep_agent, mock_toolkit, _, *_ = await self._run(flag_on=True)

        mock_toolkit.get_tools.assert_awaited()
        kwargs = mock_create_deep_agent.call_args.kwargs
        assert kwargs["tools"] == []
        middleware_types = [type(m).__name__ for m in kwargs["middleware"]]
        assert "DeferredToolsMiddleware" in middleware_types

    async def test_registers_code_review_detectors_as_subagents(self):
        # Guards the ``*load_builtin_code_review_detectors(...)`` spread at graph.py: a refactor
        # dropping it would make the code-review skill silently fall back to inline review with no
        # other failing test. The detector loader runs for real here (it is not patched), so the
        # compiled subagents handed to create_deep_agent must include every cr-* detector name.
        from automation.agent.subagents import CODE_REVIEW_DETECTOR_NAMES

        mock_create_deep_agent, _, _, *_ = await self._run(flag_on=False)

        subagents = mock_create_deep_agent.call_args.kwargs["subagents"]
        registered = {s["name"] for s in subagents if isinstance(s, dict) and "name" in s}
        assert set(CODE_REVIEW_DETECTOR_NAMES) <= registered

    async def test_threads_mcp_tools_into_general_purpose_and_custom_subagents(self):
        # P1 harness fix: a `task` delegation that needs an MCP tool fails with "command not
        # found" unless the subagent's tool registry carries the MCP toolset. The general-purpose
        # and custom-subagent builders must therefore receive the parent's mcp_tools; explore and
        # the code-review detectors stay deliberately scoped and do not.
        _, mock_toolkit, mcp_tool, mock_create_gp, mock_load_custom, mock_create_explore = await self._run(flag_on=True)

        assert mock_create_gp.call_args.kwargs["mcp_tools"] == [mcp_tool]
        assert mock_load_custom.await_args.kwargs["mcp_tools"] == [mcp_tool]
        # Explore is deliberately scoped to file search and must NOT receive the MCP toolset. Its
        # factory takes **kwargs, so a stray mcp_tools= would be silently swallowed rather than
        # error — assert the graph never passes it. (Detectors are excluded structurally: their
        # loader has no mcp_tools parameter, so passing it would raise TypeError.)
        assert "mcp_tools" not in mock_create_explore.call_args.kwargs

    async def test_main_agent_defers_with_empty_toolset_when_flag_on(self):
        # The main agent's web/git-platform tools are not in ALWAYS_LOADED_TOOLS, so
        # DeferredToolsMiddleware must still be installed even with zero MCP tools — otherwise those
        # tools would be silently eager-bound (the exact bloat deferral exists to prevent).
        mock_create_deep_agent, *_ = await self._run(flag_on=True, tools=[])

        kwargs = mock_create_deep_agent.call_args.kwargs
        assert kwargs["tools"] == []
        middleware_types = [type(m).__name__ for m in kwargs["middleware"]]
        assert "DeferredToolsMiddleware" in middleware_types


def test_output_invariants_prompt_keys_off_working_directory():
    """The output-invariants block strips the run's actual repo prefix (sandbox => /workspace/repo/)."""
    from automation.agent.graph import _output_invariants_system_prompt

    sandbox = _output_invariants_system_prompt("/workspace/repo/")
    assert '"/workspace/repo/"' in sandbox
    assert '"/repo/"' not in sandbox

    disk = _output_invariants_system_prompt("/myrepo/")
    assert '"/myrepo/"' in disk
