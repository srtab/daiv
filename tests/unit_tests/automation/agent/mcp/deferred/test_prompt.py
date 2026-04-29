from langchain_core.tools import StructuredTool

from automation.agent.mcp.deferred.index import DeferredMCPToolsIndex
from automation.agent.mcp.deferred.prompt import build_deferred_tools_block


def _make_tool(name: str, description: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda **kwargs: "ok", name=name, description=description)


class TestBuildDeferredToolsBlock:
    def test_empty_when_no_deferred_entries(self):
        index = DeferredMCPToolsIndex([])
        assert build_deferred_tools_block(index, set()) == ""

    def test_lists_unloaded_tools_with_first_description_line(self):
        tools = [
            _make_tool("github_create_issue", "Create a GitHub issue.\nMore detail follows."),
            _make_tool("sentry_find_orgs", "List Sentry organizations."),
        ]
        index = DeferredMCPToolsIndex(tools)
        block = build_deferred_tools_block(index, set())

        assert "<available-deferred-tools>" in block
        assert "</available-deferred-tools>" in block
        assert "github_create_issue: Create a GitHub issue." in block
        assert "sentry_find_orgs: List Sentry organizations." in block
        assert "More detail follows." not in block

    def test_hides_already_loaded_tools(self):
        tools = [_make_tool("github_create_issue", "Create issue"), _make_tool("sentry_find_orgs", "List orgs")]
        index = DeferredMCPToolsIndex(tools)
        block = build_deferred_tools_block(index, {"github_create_issue"})

        assert "github_create_issue" not in block
        assert "sentry_find_orgs" in block

    def test_empty_when_every_tool_loaded(self):
        tools = [_make_tool("github_create_issue", "Create issue")]
        index = DeferredMCPToolsIndex(tools)
        assert build_deferred_tools_block(index, {"github_create_issue"}) == ""

    def test_truncates_long_first_lines(self):
        long_desc = "Create something. " + ("verylong " * 100)
        tools = [_make_tool("noisy", long_desc)]
        index = DeferredMCPToolsIndex(tools)
        block = build_deferred_tools_block(index, set())
        assert "noisy: " in block
        body_line = next(line for line in block.splitlines() if line.startswith("noisy:"))
        assert len(body_line) <= len("noisy: ") + 200

    def test_skips_always_loaded_tools(self):
        tools = [_make_tool("github_create_issue", "Create issue"), _make_tool("filesystem_read_file", "Read a file")]
        index = DeferredMCPToolsIndex(tools, always_loaded={"filesystem_read_file"})
        block = build_deferred_tools_block(index, set())
        assert "github_create_issue" in block
        assert "filesystem_read_file" not in block

    def test_includes_tool_search_usage_instructions(self):
        tools = [_make_tool("github_create_issue", "Create issue")]
        index = DeferredMCPToolsIndex(tools)
        block = build_deferred_tools_block(index, set())
        assert "tool_search" in block
