from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

from automation.agent.mcp.schemas import ToolFilter
from automation.agent.mcp.toolkits import MCPToolkit, _apply_tool_filters


def _make_tool(name: str) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


class TestApplyToolFilters:
    def test_allow_mode_passes_matching_tools(self):
        tools = [_make_tool("sentry_search_issues"), _make_tool("sentry_find_projects")]
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1
        assert result[0].name == "sentry_search_issues"

    def test_allow_mode_blocks_non_matching_tools(self):
        tools = [_make_tool("sentry_delete_project")]
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 0

    def test_block_mode_removes_matching_tools(self):
        tools = [_make_tool("sentry_search_issues"), _make_tool("sentry_find_projects")]
        filters = {"sentry": ToolFilter(mode="block", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1
        assert result[0].name == "sentry_find_projects"

    def test_block_mode_passes_non_matching_tools(self):
        tools = [_make_tool("sentry_find_projects")]
        filters = {"sentry": ToolFilter(mode="block", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1

    def test_unmatched_tools_pass_through(self):
        tools = [_make_tool("custom_tool")]
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1
        assert result[0].name == "custom_tool"

    def test_empty_tools_returns_empty(self):
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters([], filters)

        assert result == []

    def test_empty_filters_returns_all_tools(self):
        tools = [_make_tool("sentry_search_issues"), _make_tool("context7_query-docs")]

        result = _apply_tool_filters(tools, {})

        assert len(result) == 2

    def test_multiple_servers_filtered_independently(self):
        tools = [
            _make_tool("sentry_search_issues"),
            _make_tool("sentry_delete_project"),
            _make_tool("context7_query-docs"),
            _make_tool("context7_resolve-library-id"),
        ]
        filters = {
            "sentry": ToolFilter(mode="allow", items=["search_issues"]),
            "context7": ToolFilter(mode="block", items=["resolve-library-id"]),
        }

        result = _apply_tool_filters(tools, filters)

        names = [t.name for t in result]
        assert "sentry_search_issues" in names
        assert "sentry_delete_project" not in names
        assert "context7_query-docs" in names
        assert "context7_resolve-library-id" not in names


class TestAopenSessionIdHandling:
    """The cross-turn resume logic: ``MCPToolkit.aopen`` reconnects to existing
    server-side sessions when a previous id is supplied, recovers from a stale
    id by opening fresh, and drops empty slots from the dict it yields back.

    We stub the low-level session opener so the test runs without a real MCP
    server but still exercises every branch of ``_open_server``.
    """

    @pytest.fixture(autouse=True)
    def mock_mcp_toolkit_aopen(self):
        """Override the conftest autouse fixture that stubs ``aopen`` so this
        class exercises the real implementation."""
        yield

    async def _run_aopen(self, *, connections, session_open_results, session_ids=None):
        """Patch the registry + per-server opener and drive ``aopen`` once.

        ``session_open_results`` is a dict mapping ``(server_name, mode)`` —
        where ``mode`` is "resume" if a session id was passed in or "fresh"
        otherwise — to ``(tools, captured_id)`` or to an ``Exception`` to raise.
        """
        captured_calls: list[tuple[str, str]] = []

        @asynccontextmanager
        async def _fake_opener(*, url, headers, terminate_on_close, initialize):
            # `initialize=False` only happens on the resume branch (per _open_server),
            # so we can use it to dispatch the right scripted result.
            mode = "fresh" if initialize else "resume"
            server_name = next(name for name, conn in connections.items() if conn["url"] == url)
            captured_calls.append((server_name, mode))
            outcome = session_open_results[(server_name, mode)]
            if isinstance(outcome, Exception):
                raise outcome

            session = MagicMock(name=f"session-{server_name}")
            get_id = lambda captured=outcome[1]: captured  # noqa: E731
            yield session, get_id

        async def _fake_load_tools(session, *, server_name, tool_name_prefix):
            # Dispatch on the most recently opened (server, mode) — this matters
            # in the recovery path where the same server is opened twice.
            outcome = session_open_results[captured_calls[-1]]
            return list(outcome[0])

        registry = MagicMock()
        registry.get_connections_and_filters.return_value = (connections, {})

        # ``aopen`` imports the registry at runtime via
        # ``from automation.agent.mcp.registry import mcp_registry`` — patch the
        # symbol at its source so the local rebinding inside the function sees the mock.
        with (
            patch("automation.agent.mcp.toolkits._open_streamable_mcp_session", _fake_opener),
            patch("automation.agent.mcp.toolkits.load_mcp_tools", side_effect=_fake_load_tools),
            patch("automation.agent.mcp.registry.mcp_registry", registry),
        ):
            async with MCPToolkit.aopen(session_ids=session_ids) as (tools, ids):
                return tools, ids, captured_calls

    async def test_fresh_open_captures_ids_when_persist_requested(self):
        connections = {"playwright": {"transport": "streamable_http", "url": "http://pw/mcp", "headers": None}}
        tools, ids, calls = await self._run_aopen(
            connections=connections,
            session_open_results={("playwright", "fresh"): ([_make_tool("playwright_browser_navigate")], "abc-123")},
            session_ids={},
        )

        assert calls == [("playwright", "fresh")]
        assert ids == {"playwright": "abc-123"}
        assert [t.name for t in tools] == ["playwright_browser_navigate"]

    async def test_resume_skips_initialize_and_keeps_id(self):
        connections = {"playwright": {"transport": "streamable_http", "url": "http://pw/mcp", "headers": None}}
        tools, ids, calls = await self._run_aopen(
            connections=connections,
            session_open_results={("playwright", "resume"): ([_make_tool("playwright_browser_snapshot")], "abc-123")},
            session_ids={"playwright": "abc-123"},
        )

        assert calls == [("playwright", "resume")]
        assert ids == {"playwright": "abc-123"}

    async def test_resume_failure_falls_back_to_fresh_and_overwrites_id(self):
        connections = {"playwright": {"transport": "streamable_http", "url": "http://pw/mcp", "headers": None}}
        tools, ids, calls = await self._run_aopen(
            connections=connections,
            session_open_results={
                ("playwright", "resume"): RuntimeError("404 Session not found"),
                ("playwright", "fresh"): ([_make_tool("playwright_browser_navigate")], "new-456"),
            },
            session_ids={"playwright": "stale-id"},
        )

        assert calls == [("playwright", "resume"), ("playwright", "fresh")]
        assert ids == {"playwright": "new-456"}, "stale id must be replaced, not preserved"

    async def test_stateless_server_does_not_pollute_ids_dict(self):
        connections = {"sentry": {"transport": "streamable_http", "url": "http://sn/mcp", "headers": None}}
        tools, ids, calls = await self._run_aopen(
            connections=connections,
            # Stateless servers (supergateway-fronted) return no Mcp-Session-Id header,
            # so the get_session_id callback yields None.
            session_open_results={("sentry", "fresh"): ([_make_tool("sentry_whoami")], None)},
            session_ids={},
        )

        assert ids == {}, "an empty id slot would make the next turn attempt a doomed resume"

    async def test_no_session_ids_means_no_persistence_mode(self):
        connections = {"playwright": {"transport": "streamable_http", "url": "http://pw/mcp", "headers": None}}
        tools, ids, calls = await self._run_aopen(
            connections=connections,
            session_open_results={("playwright", "fresh"): ([_make_tool("playwright_browser_navigate")], "abc-123")},
            session_ids=None,
        )

        assert ids == {}, "session_ids=None signals one-shot mode; returned dict is empty"
