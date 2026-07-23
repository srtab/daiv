import pytest

from automation.agent.middlewares.submit_findings import (
    SUBMIT_FINDINGS_TOOL_NAME,
    SUBMITTED_MARKER,
    build_submit_findings_tool,
)

# Minimal stand-in for the real DetectorFindings schema — tests cover OUR handler's
# success/error contract, not jsonschema itself.
_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"detector": {"type": "string"}},
                "required": ["detector"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


class TestBuildSubmitFindingsTool:
    def test_tool_identity_and_schema(self):
        tool = build_submit_findings_tool(_SCHEMA)
        assert tool.name == SUBMIT_FINDINGS_TOOL_NAME
        # The model must see the full findings schema as the tool's args — that is how it
        # learns the finding shape now that response_format is gone.
        assert tool.args_schema == _SCHEMA

    def test_valid_payload_returns_marker_and_count(self):
        tool = build_submit_findings_tool(_SCHEMA)
        result = tool.invoke({"findings": [{"detector": "performance"}, {"detector": "performance"}]})
        assert result.startswith(SUBMITTED_MARKER)
        assert "2 finding(s)" in result

    def test_empty_findings_list_is_a_valid_submission(self):
        tool = build_submit_findings_tool(_SCHEMA)
        result = tool.invoke({"findings": []})
        assert result.startswith(SUBMITTED_MARKER)
        assert "0 finding(s)" in result

    def test_invalid_payload_returns_validation_error_not_marker(self):
        tool = build_submit_findings_tool(_SCHEMA)
        result = tool.invoke({"findings": [{"unexpected": True}]})
        assert not result.startswith(SUBMITTED_MARKER)
        assert "Validation failed" in result
        assert SUBMIT_FINDINGS_TOOL_NAME in result  # tells the model to retry the same tool

    @pytest.mark.skip(reason="enabled in Task 4")
    def test_real_detector_schema_accepts_empty_findings(self):
        # Pin the integration with the real skill schema: the wrapped object schema from
        # subagents.py must at minimum accept the empty submission.
        from automation.agent.subagents import _load_detector_findings_schema

        tool = build_submit_findings_tool(_load_detector_findings_schema())
        assert tool.invoke({"findings": []}).startswith(SUBMITTED_MARKER)
