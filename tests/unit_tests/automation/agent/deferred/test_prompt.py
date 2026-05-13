from langchain_core.tools import StructuredTool

from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.prompt import build_deferred_tools_block


def _make_tool(name: str, description: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda **kwargs: "ok", name=name, description=description)


class TestBuildDeferredToolsBlock:
    def test_empty_when_no_deferred_entries(self):
        index = DeferredToolsIndex([])
        assert build_deferred_tools_block(index) == ""

    def test_lists_deferred_tools_with_first_description_line(self):
        tools = [
            _make_tool("github_create_issue", "Create a GitHub issue.\nMore detail follows."),
            _make_tool("sentry_find_orgs", "List Sentry organizations."),
        ]
        index = DeferredToolsIndex(tools)
        block = build_deferred_tools_block(index)

        assert "<available-deferred-tools>" in block
        assert "</available-deferred-tools>" in block
        assert "github_create_issue: Create a GitHub issue." in block
        assert "sentry_find_orgs: List Sentry organizations." in block
        assert "More detail follows." not in block

    def test_block_is_stable_regardless_of_loaded_state(self):
        # Cache stability invariant: the block must be byte-identical across calls
        # within a session, otherwise the system message hash changes and Anthropic's
        # prompt cache invalidates from byte 0.
        tools = [_make_tool("github_create_issue", "Create issue"), _make_tool("sentry_find_orgs", "List orgs")]
        index = DeferredToolsIndex(tools)
        assert build_deferred_tools_block(index) == build_deferred_tools_block(index)

    def test_truncates_long_first_lines(self):
        long_desc = "Create something. " + ("verylong " * 100)
        tools = [_make_tool("noisy", long_desc)]
        index = DeferredToolsIndex(tools)
        block = build_deferred_tools_block(index)
        assert "noisy: " in block
        body_line = next(line for line in block.splitlines() if line.startswith("noisy:"))
        assert len(body_line) <= len("noisy: ") + 200

    def test_includes_tool_search_usage_instructions(self):
        tools = [_make_tool("github_create_issue", "Create issue")]
        index = DeferredToolsIndex(tools)
        block = build_deferred_tools_block(index)
        assert "tool_search" in block
