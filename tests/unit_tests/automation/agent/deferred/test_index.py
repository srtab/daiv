from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from automation.agent.deferred.index import DeferredToolsIndex


class _GitHubArgs(BaseModel):
    owner: str = Field(description="The repository owner")
    repo: str = Field(description="The repository name")
    title: str = Field(description="The pull request title")


def _make_tool(name: str, description: str, args_schema: type[BaseModel] | None = None) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda **kwargs: "ok", name=name, description=description, args_schema=args_schema
    )


class TestDeferredToolsIndex:
    def test_get_returns_entry_for_known_tool(self):
        tool = _make_tool("github_create_issue", "Create a GitHub issue")
        index = DeferredToolsIndex([tool])
        entry = index.get("github_create_issue")
        assert entry is not None
        assert entry.name == "github_create_issue"
        assert entry.tool is tool

    def test_get_returns_none_for_unknown_tool(self):
        index = DeferredToolsIndex([])
        assert index.get("nope") is None

    def test_search_ranks_relevant_tool_first(self):
        tools = [
            _make_tool("sentry_find_organizations", "List organizations in Sentry"),
            _make_tool("github_create_pull_request", "Open a pull request on GitHub", _GitHubArgs),
            _make_tool("gitlab_get_merge_request", "Fetch a merge request from GitLab"),
        ]
        index = DeferredToolsIndex(tools)
        results = index.search("create github pull request", top_k=3)
        assert results, "expected at least one result"
        assert results[0].name == "github_create_pull_request"

    def test_search_returns_empty_for_empty_query(self):
        index = DeferredToolsIndex([_make_tool("github_create_issue", "Create issue")])
        assert index.search("", top_k=5) == []
        assert index.search("   ", top_k=5) == []

    def test_search_returns_empty_when_query_only_stopwords(self):
        tools = [_make_tool("github_create_issue", "Create issue")]
        index = DeferredToolsIndex(tools)
        results = index.search("the a of", top_k=5)
        assert results == []

    def test_deferred_entries_returns_all_indexed_tools(self):
        a = _make_tool("github_create_issue", "Create issue")
        b = _make_tool("github_get_user", "Get user")
        index = DeferredToolsIndex([a, b])
        names = sorted(e.name for e in index.deferred_entries())
        assert names == ["github_create_issue", "github_get_user"]

    def test_duplicate_names_deduplicated(self):
        a = _make_tool("github_create_issue", "Create issue")
        b = _make_tool("github_create_issue", "Different description")
        index = DeferredToolsIndex([a, b])
        assert len(index.deferred_entries()) == 1
        assert index.get("github_create_issue").tool is a

    def test_args_schema_text_indexed(self):
        tools = [
            _make_tool("github_create_pull_request", "Open a PR", _GitHubArgs),
            _make_tool("sentry_list_orgs", "List organizations"),
        ]
        index = DeferredToolsIndex(tools)
        results = index.search("repository owner", top_k=2)
        assert results
        assert results[0].name == "github_create_pull_request"

    def test_indexed_text_capped(self):
        tool = _make_tool("noisy_tool", "x" * 5000)
        index = DeferredToolsIndex([tool])
        entry = index.get("noisy_tool")
        assert entry is not None
        assert len(entry.indexed_text) <= 2048

    def test_args_schema_with_unserializable_field_degrades_gracefully(self):
        # Mirrors real-world tools (e.g. GitPython-backed) whose args_schema includes a non-Pydantic
        # type — model_json_schema() raises PydanticInvalidForJsonSchema; indexing must not fail.
        class _OpaqueRepo:
            pass

        class _RepoArgs(BaseModel):
            model_config = {"arbitrary_types_allowed": True}

            repo: _OpaqueRepo = Field(description="An opaque repo handle")

        tool = _make_tool("repo_tool", "Operate on a repo", _RepoArgs)
        index = DeferredToolsIndex([tool])

        entry = index.get("repo_tool")
        assert entry is not None
        assert "repo" in entry.indexed_text and "tool" in entry.indexed_text

    def test_search_drops_weak_matches_below_relative_score_floor(self):
        # Reproduces the production trace where query "gitlab merge request list open"
        # leaked sentry_list_* and context7 tools via single-token overlap on "list"/"open".
        tools = [
            _make_tool("gitlab", "Inspect GitLab merge requests, issues, pipelines"),
            _make_tool("sentry_list_issues", "List issues using Sentry query syntax"),
            _make_tool("sentry_list_events", "List events or replays using Sentry query syntax"),
            _make_tool("context7_resolve_library_id", "Resolve a package name to a library id"),
        ]
        index = DeferredToolsIndex(tools)
        results = index.search("gitlab merge request list open", top_k=5)
        names = [r.name for r in results]
        assert names == ["gitlab"], f"expected only gitlab, got {names}"

    def test_search_keeps_single_token_query(self):
        # Single-token queries must still return their match — relative gate adapts to query length.
        tools = [
            _make_tool("context7_resolve_library_id", "Resolve a package name to a library id"),
            _make_tool("gitlab", "Inspect GitLab merge requests"),
            _make_tool("sentry_list_issues", "List issues using Sentry query syntax"),
        ]
        index = DeferredToolsIndex(tools)
        results = index.search("context7", top_k=5)
        assert [r.name for r in results] == ["context7_resolve_library_id"]

    def test_args_schema_without_model_json_schema_degrades_gracefully(self):
        tool = _make_tool("plain_tool", "A plain tool")

        class _LegacySchema:
            pass

        tool.args_schema = _LegacySchema
        index = DeferredToolsIndex([tool])

        entry = index.get("plain_tool")
        assert entry is not None
        assert "plain" in entry.indexed_text and "tool" in entry.indexed_text
