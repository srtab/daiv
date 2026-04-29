from unittest.mock import AsyncMock, MagicMock, patch


def _common_patches():
    """Patches that disable side effects unrelated to the deferred-tools wiring."""
    return [
        patch("automation.agent.graph.compute_summarization_defaults"),
        patch("automation.agent.graph.FilesystemBackend"),
        patch("automation.agent.graph.create_general_purpose_subagent"),
        patch("automation.agent.graph.create_explore_subagent"),
        patch("automation.agent.graph.load_custom_subagents", AsyncMock(return_value=[])),
        patch("automation.agent.graph.create_agent"),
        patch("automation.agent.graph.MCPToolkit"),
        patch("automation.agent.graph.mcp_settings"),
        patch("automation.agent.graph.BaseAgent"),
        patch("automation.agent.graph.site_settings"),
        # Middleware constructors that validate input — replaced with no-op mocks
        patch("automation.agent.graph.SubAgentMiddleware"),
        patch("automation.agent.graph.MemoryMiddleware"),
        patch("automation.agent.graph.SkillsMiddleware"),
        patch("automation.agent.graph.TodoListMiddleware"),
        patch("automation.agent.graph.SummarizationMiddleware"),
        patch("automation.agent.graph.AnthropicPromptCachingMiddleware"),
        patch("automation.agent.graph.PatchToolCallsMiddleware"),
        patch("automation.agent.graph.FilesystemMiddleware"),
        patch("automation.agent.graph.GitMiddleware"),
        patch("automation.agent.graph.GitPlatformMiddleware"),
        patch("automation.agent.graph.ToolCallLoggingMiddleware"),
    ]


class TestCreateDaivAgentDeferredFlag:
    """The DEFERRED_TOOLS_ENABLED flag toggles tool-binding strategy at agent build time."""

    async def _run(self, *, flag_on: bool):
        from langchain_core.tools import StructuredTool

        from automation.agent.graph import create_daiv_agent

        patches = _common_patches()
        managers = [p.start() for p in patches]
        try:
            (
                mock_summ,
                mock_fs_backend,
                mock_create_gp,
                mock_create_explore,
                mock_load_custom,
                mock_create_agent,
                mock_toolkit,
                mock_mcp_settings,
                mock_base_agent,
                mock_site_settings,
                *_,
            ) = managers

            mock_mcp_settings.DEFERRED_TOOLS_ENABLED = flag_on
            mock_mcp_settings.DEFERRED_TOOLS_TOP_K_DEFAULT = 5
            mock_mcp_settings.DEFERRED_TOOLS_TOP_K_MAX = 10

            mock_toolkit.get_tools = AsyncMock(
                return_value=[StructuredTool.from_function(func=lambda **k: "x", name="t1", description="d")]
            )
            mock_toolkit.aget_deferred_index = AsyncMock(return_value=MagicMock())

            mock_base_agent.get_model.return_value = MagicMock()
            mock_summ.return_value = {
                "trigger": MagicMock(),
                "keep": MagicMock(),
                "truncate_args_settings": MagicMock(),
            }

            mock_site_settings.agent_recursion_limit = 50
            mock_site_settings.agent_model_name = "claude"
            mock_site_settings.agent_fallback_model_name = "claude"
            mock_site_settings.agent_thinking_level = None
            mock_site_settings.web_fetch_enabled = False
            mock_site_settings.web_search_enabled = False

            mock_create_agent.return_value.with_config.return_value = MagicMock()

            ctx = MagicMock()
            ctx.gitrepo.working_dir = "/repo"
            ctx.config.sandbox.enabled = False
            ctx.config.context_file_name = "AGENTS.md"
            ctx.git_platform = MagicMock()

            await create_daiv_agent(ctx=ctx, auto_commit_changes=False)
            return mock_create_agent, mock_toolkit
        finally:
            for p in patches:
                p.stop()

    async def test_flag_off_passes_eager_tools(self):
        mock_create_agent, mock_toolkit = await self._run(flag_on=False)

        mock_toolkit.aget_deferred_index.assert_not_awaited()
        kwargs = mock_create_agent.call_args.kwargs
        assert len(kwargs["tools"]) == 1
        middleware_types = [type(m).__name__ for m in kwargs["middleware"]]
        assert "DeferredMCPToolsMiddleware" not in middleware_types

    async def test_flag_on_passes_empty_tools_and_adds_middleware(self):
        mock_create_agent, mock_toolkit = await self._run(flag_on=True)

        mock_toolkit.get_tools.assert_not_awaited()
        kwargs = mock_create_agent.call_args.kwargs
        assert kwargs["tools"] == []
        middleware_types = [type(m).__name__ for m in kwargs["middleware"]]
        assert "DeferredMCPToolsMiddleware" in middleware_types
