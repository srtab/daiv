from unittest.mock import MagicMock

import pytest

from automation.agent.deferred.coercion import decode_stringified_args

_SCHEMA = {
    "type": "object",
    "properties": {
        "monitorId": {"type": "integer"},
        "monitorIds": {"type": "array", "items": {"type": "integer"}},
        "filter": {"type": "object", "properties": {"minDuration": {"type": "integer"}}},
        "ratio": {"type": "number"},
        "includeResolved": {"type": "boolean"},
        "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "pageSize": {"type": ["integer", "null"]},
        "status": {"oneOf": [{"type": "integer"}, {"type": "boolean"}]},
        "cursor": {"type": ["string", "null"]},
        "version": {"type": ["string", "number"]},
        "timeRange": {"type": "string"},
        "mode": {"enum": [1, 2]},
        "window": {"anyOf": [{"type": "integer"}, {"enum": ["24h", "7d"]}]},
    },
}


class TestDecodeStringifiedArgs:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            pytest.param(
                {"monitorId": "784007076", "ratio": "0.5", "includeResolved": "true"},
                {"monitorId": 784007076, "ratio": 0.5, "includeResolved": True},
                id="scalars",
            ),
            pytest.param(
                {"monitorIds": "[11, 22]", "filter": '{"minDuration": 300}'},
                {"monitorIds": [11, 22], "filter": {"minDuration": 300}},
                id="array-and-object",
            ),
            pytest.param({"limit": "20"}, {"limit": 20}, id="nullable-any-of"),
            pytest.param({"limit": "null"}, {"limit": None}, id="nullable-any-of-null"),
            pytest.param({"pageSize": "20"}, {"pageSize": 20}, id="nullable-type-list"),
            pytest.param({"status": "false"}, {"status": False}, id="one-of"),
            pytest.param({"monitorId": "7", "timeRange": "30"}, {"monitorId": 7}, id="only-decoded-keys-returned"),
        ],
    )
    def test_decodes(self, args, expected):
        assert decode_stringified_args(args, _SCHEMA) == expected

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param({"cursor": "null", "version": "1.10", "timeRange": "30"}, id="schema-allows-string"),
            pytest.param(
                {"monitorId": "1.5", "includeResolved": "1", "ratio": "abc", "monitorIds": '{"a": 1}'},
                id="decoded-type-mismatch",
            ),
            pytest.param({"ratio": "NaN"}, id="nan"),
            pytest.param({"ratio": "Infinity"}, id="infinity"),
            pytest.param({"ratio": "-Infinity"}, id="negative-infinity"),
            pytest.param({"ratio": "1e400"}, id="float-overflow"),
            pytest.param({"monitorIds": "[NaN]"}, id="nested-nan"),
            pytest.param({"monitorId": "1" * 5000, "monitorIds": "1" * 5000}, id="int-too-long"),
            pytest.param({"mode": "1", "window": "24", "unknown": "5"}, id="untyped-or-unknown-property"),
            pytest.param({"monitorId": 784007076, "monitorIds": [11], "includeResolved": False}, id="already-typed"),
        ],
    )
    def test_keeps_as_sent(self, args):
        assert decode_stringified_args(args, _SCHEMA) == {}

    @pytest.mark.parametrize(
        "prop", [{"anyOf": [{"type": "integer"}, True]}, {"anyOf": {"type": "integer"}}, {"type": ["integer", 1]}, True]
    )
    def test_keeps_value_for_boolean_or_malformed_subschema(self, prop):
        schema = {"type": "object", "properties": {"n": prop}}

        assert decode_stringified_args({"n": "5"}, schema) == {}

    def test_keeps_value_when_decoding_exceeds_the_stack(self, monkeypatch):
        from automation.agent.deferred import coercion

        monkeypatch.setattr(coercion.json, "loads", MagicMock(side_effect=RecursionError))

        assert decode_stringified_args({"monitorIds": "[[[1]]]"}, _SCHEMA) == {}

    def test_keeps_args_when_properties_is_not_a_mapping(self):
        assert decode_stringified_args({"n": "5"}, {"type": "object", "properties": ["n"]}) == {}
