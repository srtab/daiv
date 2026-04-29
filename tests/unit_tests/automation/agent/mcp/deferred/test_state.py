from automation.agent.mcp.deferred.state import union_loaded_tool_names


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
