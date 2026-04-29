import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from automation.agent.mcp.deferred.index import DeferredMCPToolsIndex


class _GitHubArgs(BaseModel):
    owner: str = Field(description="The repository owner")
    repo: str = Field(description="The repository name")
    title: str = Field(description="The pull request title")


def _make_tool(name: str, description: str, args_schema: type[BaseModel] | None = None) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda **kwargs: "ok", name=name, description=description, args_schema=args_schema
    )


class TestDeferredMCPToolsIndex:
    def test_get_returns_entry_for_known_tool(self):
        tool = _make_tool("github_create_issue", "Create a GitHub issue")
        index = DeferredMCPToolsIndex([tool])
        entry = index.get("github_create_issue")
        assert entry is not None
        assert entry.name == "github_create_issue"
        assert entry.tool is tool

    def test_get_returns_none_for_unknown_tool(self):
        index = DeferredMCPToolsIndex([])
        assert index.get("nope") is None

    def test_search_ranks_relevant_tool_first(self):
        tools = [
            _make_tool("sentry_find_organizations", "List organizations in Sentry"),
            _make_tool("github_create_pull_request", "Open a pull request on GitHub", _GitHubArgs),
            _make_tool("gitlab_get_merge_request", "Fetch a merge request from GitLab"),
        ]
        index = DeferredMCPToolsIndex(tools)
        results = index.search("create github pull request", top_k=3)
        assert results, "expected at least one result"
        assert results[0].name == "github_create_pull_request"

    def test_search_returns_empty_for_empty_query(self):
        index = DeferredMCPToolsIndex([_make_tool("github_create_issue", "Create issue")])
        assert index.search("", top_k=5) == []
        assert index.search("   ", top_k=5) == []

    def test_search_returns_empty_when_query_only_stopwords(self):
        tools = [_make_tool("github_create_issue", "Create issue")]
        index = DeferredMCPToolsIndex(tools)
        results = index.search("the a of", top_k=5)
        assert results == []

    def test_always_loaded_tools_filter(self):
        a = _make_tool("github_create_issue", "Create issue")
        b = _make_tool("github_get_user", "Get user")
        index = DeferredMCPToolsIndex([a, b], always_loaded={"github_get_user"})
        always = index.always_loaded_tools()
        deferred = [e.name for e in index.deferred_entries()]
        assert [t.name for t in always] == ["github_get_user"]
        assert deferred == ["github_create_issue"]

    def test_always_loaded_unknown_name_raises(self):
        a = _make_tool("github_create_issue", "Create issue")
        with pytest.raises(ValueError, match="does_not_exist"):
            DeferredMCPToolsIndex([a], always_loaded={"does_not_exist"})

    def test_args_schema_text_indexed(self):
        tools = [
            _make_tool("github_create_pull_request", "Open a PR", _GitHubArgs),
            _make_tool("sentry_list_orgs", "List organizations"),
        ]
        index = DeferredMCPToolsIndex(tools)
        results = index.search("repository owner", top_k=2)
        assert results
        assert results[0].name == "github_create_pull_request"

    def test_indexed_text_capped(self):
        tool = _make_tool("noisy_tool", "x" * 5000)
        index = DeferredMCPToolsIndex([tool])
        entry = index.get("noisy_tool")
        assert entry is not None
        assert len(entry.indexed_text) <= 2048

    def test_args_schema_without_model_json_schema_degrades_gracefully(self):
        tool = _make_tool("plain_tool", "A plain tool")

        class _LegacySchema:
            pass

        tool.args_schema = _LegacySchema
        index = DeferredMCPToolsIndex([tool])

        entry = index.get("plain_tool")
        assert entry is not None
        assert "plain" in entry.indexed_text and "tool" in entry.indexed_text
