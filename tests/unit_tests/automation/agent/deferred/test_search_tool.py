from unittest.mock import Mock

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.search_tool import make_tool_search


def _make_tool(name: str, description: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda **kwargs: "ok", name=name, description=description)


def _runtime(state: dict | None = None) -> ToolRuntime:
    return ToolRuntime(
        state=state or {"loaded_tool_names": set()},
        context=Mock(),
        config={},
        stream_writer=Mock(),
        tool_call_id="call_123",
        store=None,
    )


class TestToolSearch:
    async def test_search_loads_top_k(self):
        tools = [
            _make_tool("github_create_issue", "Create a GitHub issue"),
            _make_tool("sentry_find_orgs", "List Sentry organizations"),
        ]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "github issue", "runtime": _runtime()})

        assert isinstance(result, Command)
        assert "github_create_issue" in result.update["loaded_tool_names"]
        msg = result.update["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert msg.tool_call_id == "call_123"
        assert "Loaded" in msg.content
        assert "github_create_issue" in msg.content

    async def test_search_with_no_results_returns_message_only(self):
        index = DeferredToolsIndex([_make_tool("github_create_issue", "Create issue")])
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "totally_unrelated_xyzzy", "runtime": _runtime()})

        assert isinstance(result, Command)
        assert "loaded_tool_names" not in result.update
        msg = result.update["messages"][0]
        assert "totally_unrelated_xyzzy" in msg.content

    async def test_select_loads_exact_names(self):
        tools = [
            _make_tool("github_create_issue", "Create a GitHub issue"),
            _make_tool("sentry_find_orgs", "List Sentry organizations"),
        ]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": ["sentry_find_orgs"], "runtime": _runtime()})

        assert isinstance(result, Command)
        assert result.update["loaded_tool_names"] == {"sentry_find_orgs"}

    async def test_select_unknown_name_surfaces_in_message(self):
        index = DeferredToolsIndex([_make_tool("github_create_issue", "Create issue")])
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({
            "query": "",
            "select": ["does_not_exist", "github_create_issue"],
            "runtime": _runtime(),
        })

        assert isinstance(result, Command)
        assert result.update["loaded_tool_names"] == {"github_create_issue"}
        assert "does_not_exist" in result.update["messages"][0].content

    async def test_select_all_unknown_returns_dedicated_message(self):
        index = DeferredToolsIndex([_make_tool("github_create_issue", "Create issue")])
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": ["nope_a", "nope_b"], "runtime": _runtime()})

        assert "loaded_tool_names" not in result.update
        msg_content = result.update["messages"][0].content
        assert "nope_a" in msg_content and "nope_b" in msg_content

    async def test_reads_existing_loaded_state(self):
        tools = [_make_tool("github_create_issue", "Create issue"), _make_tool("sentry_find_orgs", "List orgs")]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        runtime = _runtime({"loaded_tool_names": {"sentry_find_orgs"}})
        result = await tool_search.ainvoke({"query": "", "select": ["github_create_issue"], "runtime": runtime})

        assert result.update["loaded_tool_names"] == {"sentry_find_orgs", "github_create_issue"}

    async def test_top_k_clamped_to_max(self):
        tools = [_make_tool(f"helper_tool_{i}", f"helper number {i}") for i in range(20)]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=3)

        result = await tool_search.ainvoke({"query": "helper", "top_k": 50, "runtime": _runtime()})

        assert isinstance(result, Command)
        assert len(result.update["loaded_tool_names"]) <= 3

    async def test_select_accepts_json_string_list(self):
        # Qwen 3.x has been observed to JSON-stringify the `select` array.
        tools = [_make_tool("github_create_issue", "Create a GitHub issue")]
        tool_search = make_tool_search(lambda: DeferredToolsIndex(tools), top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": '["github_create_issue"]', "runtime": _runtime()})

        assert isinstance(result, Command)
        assert result.update["loaded_tool_names"] == {"github_create_issue"}

    async def test_select_accepts_bare_string_name(self):
        tools = [_make_tool("github_create_issue", "Create a GitHub issue")]
        tool_search = make_tool_search(lambda: DeferredToolsIndex(tools), top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": "github_create_issue", "runtime": _runtime()})

        assert isinstance(result, Command)
        assert result.update["loaded_tool_names"] == {"github_create_issue"}

    async def test_select_non_list_json_falls_back_to_single_name(self):
        # ``json.loads("42")`` succeeds but returns an int, not a list. The coercion
        # wraps the raw string as a single-name shorthand — the downstream "unknown
        # name" branch then gives the model a clean signal instead of looping on
        # validation errors.
        tool_search = make_tool_search(lambda: DeferredToolsIndex([]), top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": "42", "runtime": _runtime()})

        assert isinstance(result, Command)
        assert "loaded_tool_names" not in result.update
        assert "42" in result.update["messages"][0].content


class TestToolSearchSchemaDelivery:
    def _index_with_typed_tool(self):
        from pydantic import BaseModel, Field

        class _Args(BaseModel):
            ticket: str = Field(description="Ticket identifier.")
            max_notes: int = Field(default=5, description="Max correspondence notes.")

        def _run(ticket: str, max_notes: int = 5) -> str:
            return "ok"

        tool = StructuredTool.from_function(
            func=_run, name="rt_fetch_ticket_digest", description="Fetch a ticket digest.", args_schema=_Args
        )
        return DeferredToolsIndex([tool])

    async def test_result_embeds_full_schema_in_functions_block(self):
        tool_search = make_tool_search(lambda: self._index_with_typed_tool(), top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": ["rt_fetch_ticket_digest"], "runtime": _runtime()})

        content = result.update["messages"][0].content
        assert "<functions>" in content and "</functions>" in content
        assert "rt_fetch_ticket_digest" in content
        # The schema's real parameter names ride in the result — this is what a frozen-array model reads.
        assert "max_notes" in content
        assert "ticket" in content

    async def test_reselecting_loaded_name_resends_schema(self):
        # Idempotent re-delivery: a summarization-evicted (or migration-inherited) schema is
        # recoverable by re-searching. No short-circuit on already-loaded names.
        tool_search = make_tool_search(lambda: self._index_with_typed_tool(), top_k_default=5, top_k_max=10)
        runtime = _runtime({"loaded_tool_names": {"rt_fetch_ticket_digest"}})

        result = await tool_search.ainvoke({"query": "", "select": ["rt_fetch_ticket_digest"], "runtime": runtime})

        content = result.update["messages"][0].content
        assert "<functions>" in content
        assert "max_notes" in content
        assert result.update["loaded_tool_names"] == {"rt_fetch_ticket_digest"}

    async def test_valve_off_returns_summaries_only(self, monkeypatch):
        from automation.agent.deferred import search_tool as search_tool_module

        monkeypatch.setattr(search_tool_module.deferred_settings, "EMBED_SCHEMAS_IN_RESULTS", False)
        tool_search = make_tool_search(lambda: self._index_with_typed_tool(), top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": ["rt_fetch_ticket_digest"], "runtime": _runtime()})

        content = result.update["messages"][0].content
        assert "<functions>" not in content
        assert "rt_fetch_ticket_digest" in content
        assert "Fetch a ticket digest." in content

    def _index_with_unconvertible_tool(self):
        from pydantic import BaseModel, ConfigDict, Field

        class _Unserializable:
            pass

        class _Args(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            repo: _Unserializable = Field(description="Non-serializable handle.")

        def _run(repo) -> str:
            return "ok"

        tool = StructuredTool.from_function(
            func=_run, name="bad_tool", description="Tool with a non-serializable arg.", args_schema=_Args
        )
        return DeferredToolsIndex([tool])

    async def test_unconvertible_tool_degrades_to_summary_and_warns(self, caplog):
        # A tool whose args reference a non-serializable type (mirrors index.py's git.Repo case)
        # can't emit a JSON schema: the result degrades to a <function-summary> rather than aborting,
        # and warns (not debug) because on a frozen model that tool is then uncallable.
        import logging

        tool_search = make_tool_search(lambda: self._index_with_unconvertible_tool(), top_k_default=5, top_k_max=10)

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            result = await tool_search.ainvoke({"query": "", "select": ["bad_tool"], "runtime": _runtime()})

        content = result.update["messages"][0].content
        assert "<function-summary>" in content
        assert "bad_tool" in content
        assert "<function>" not in content
        assert result.update["loaded_tool_names"] == {"bad_tool"}
        assert any(r.levelname == "WARNING" and "bad_tool" in r.getMessage() for r in caplog.records)

    async def test_unexpected_conversion_error_is_contained_per_tool(self, monkeypatch, caplog):
        # A non-Pydantic conversion fault (ValueError/TypeError from convert_to_openai_tool) must not
        # abort the whole batch: the offending tool degrades to a summary, siblings still load, and it
        # logs at error level (unlike the known git.Repo case, which warns).
        import logging

        from automation.agent.deferred import search_tool as search_tool_module

        good = StructuredTool.from_function(func=lambda ticket: "ok", name="good_tool", description="Converts fine.")
        bad = StructuredTool.from_function(
            func=lambda ticket: "ok", name="explodes", description="Trips an unexpected fault."
        )
        index = DeferredToolsIndex([good, bad])

        real = search_tool_module.convert_to_openai_tool

        def _flaky(tool):
            if tool.name == "explodes":
                raise ValueError("simulated non-pydantic conversion fault")
            return real(tool)

        monkeypatch.setattr(search_tool_module, "convert_to_openai_tool", _flaky)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        with caplog.at_level(logging.ERROR, logger="daiv.tools"):
            result = await tool_search.ainvoke({
                "query": "",
                "select": ["good_tool", "explodes"],
                "runtime": _runtime(),
            })

        content = result.update["messages"][0].content
        assert "<function>" in content  # the good tool still loaded despite the sibling's failure
        assert "<function-summary>explodes:" in content
        assert result.update["loaded_tool_names"] == {"good_tool", "explodes"}
        assert any(r.levelname == "ERROR" and "explodes" in r.getMessage() for r in caplog.records)
