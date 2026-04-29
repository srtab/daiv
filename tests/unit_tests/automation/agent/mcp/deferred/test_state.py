from automation.agent.mcp.deferred.state import DeferredMCPToolsState, union_loaded_tool_names


class TestUnionLoadedToolNames:
    def test_left_none_right_set(self):
        assert union_loaded_tool_names(None, {"a", "b"}) == {"a", "b"}

    def test_left_set_right_none(self):
        assert union_loaded_tool_names({"a"}, None) == {"a"}

    def test_both_none(self):
        assert union_loaded_tool_names(None, None) == set()

    def test_disjoint_union(self):
        assert union_loaded_tool_names({"a"}, {"b"}) == {"a", "b"}

    def test_overlapping_union(self):
        assert union_loaded_tool_names({"a", "b"}, {"b", "c"}) == {"a", "b", "c"}


class TestDeferredMCPToolsState:
    def test_state_can_omit_loaded_tool_names(self):
        # NotRequired — empty dict is a valid instance.
        state: DeferredMCPToolsState = {"messages": []}
        assert state.get("loaded_tool_names") is None

    def test_state_accepts_set(self):
        state: DeferredMCPToolsState = {"messages": [], "loaded_tool_names": {"sentry_find_organizations"}}
        assert state["loaded_tool_names"] == {"sentry_find_organizations"}
