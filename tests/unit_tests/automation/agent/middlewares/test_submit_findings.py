from unittest.mock import Mock

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse, ToolCallRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.middlewares.submit_findings import (
    BATCHED_SUBMIT_REJECTION,
    FINALIZE_NUDGE,
    MAX_FINALIZE_NUDGES,
    SUBMIT_FINDINGS_DESCRIPTION,
    SUBMIT_FINDINGS_TOOL_NAME,
    SUBMITTED_MARKER,
    SubmitFindingsEnforcerMiddleware,
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

    def test_real_detector_schema_accepts_empty_findings(self):
        # Pin the integration with the real skill schema: the wrapped object schema from
        # subagents.py must at minimum accept the empty submission.
        from automation.agent.subagents import _load_detector_findings_schema

        tool = build_submit_findings_tool(_load_detector_findings_schema())
        assert tool.invoke({"findings": []}).startswith(SUBMITTED_MARKER)


def _request(messages: list) -> ModelRequest:
    return ModelRequest(model=GenericFakeChatModel(messages=iter([])), messages=messages)


def _submit_call_history() -> list:
    """History where a successful submit_findings round-trip already happened."""
    return [
        HumanMessage(content="audit this"),
        AIMessage(
            content="", tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": []}, "id": "call-1"}]
        ),
        ToolMessage(
            content=f"{SUBMITTED_MARKER} (0 finding(s)).", name=SUBMIT_FINDINGS_TOOL_NAME, tool_call_id="call-1"
        ),
    ]


def _handler_returning(*responses: ModelResponse):
    """Async handler yielding the given responses in order, recording the requests it saw."""
    seen: list[ModelRequest] = []
    queue = list(responses)

    async def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    handler.seen = seen
    return handler


class TestSubmitFindingsEnforcerMiddleware:
    async def test_tool_calling_response_passes_through(self):
        response = ModelResponse(
            result=[AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "x"}, "id": "c1"}])]
        )
        handler = _handler_returning(response)

        result = await SubmitFindingsEnforcerMiddleware().awrap_model_call(
            _request([HumanMessage(content="audit")]), handler
        )

        assert result is response
        assert len(handler.seen) == 1  # no retries

    async def test_text_finish_after_submit_passes_through(self):
        response = ModelResponse(result=[AIMessage(content="Done: no issues found.")])
        handler = _handler_returning(response)

        result = await SubmitFindingsEnforcerMiddleware().awrap_model_call(_request(_submit_call_history()), handler)

        assert result is response
        assert len(handler.seen) == 1

    async def test_text_finish_without_submit_is_nudged_then_returned(self):
        text_only = ModelResponse(result=[AIMessage(content="My findings: everything is fine.")])
        handler = _handler_returning(text_only)

        result = await SubmitFindingsEnforcerMiddleware().awrap_model_call(
            _request([HumanMessage(content="audit")]), handler
        )

        # 1 initial call + MAX_FINALIZE_NUDGES retries, each retry carrying the ephemeral nudge.
        assert len(handler.seen) == 1 + MAX_FINALIZE_NUDGES
        for retry_request in handler.seen[1:]:
            assert SUBMIT_FINDINGS_TOOL_NAME in retry_request.messages[-1].content
        # Gives up gracefully: the text response flows out (degrades to .txt deferral = failed detector).
        assert result is text_only

    async def test_nudge_retry_stops_as_soon_as_model_calls_a_tool(self):
        text_only = ModelResponse(result=[AIMessage(content="done?")])
        submits = ModelResponse(
            result=[
                AIMessage(
                    content="", tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": []}, "id": "c9"}]
                )
            ]
        )
        handler = _handler_returning(text_only, submits)

        result = await SubmitFindingsEnforcerMiddleware().awrap_model_call(
            _request([HumanMessage(content="audit")]), handler
        )

        assert result is submits
        assert len(handler.seen) == 2

    async def test_tool_calls_after_submit_are_finalized(self):
        # A model that resumes reading files after recording findings is a leak — the run must end.
        response = ModelResponse(
            result=[AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "x"}, "id": "c2"}])]
        )
        handler = _handler_returning(response)

        result = await SubmitFindingsEnforcerMiddleware().awrap_model_call(_request(_submit_call_history()), handler)

        assert isinstance(result, AIMessage)
        assert not result.tool_calls
        assert "already submitted" in result.content

    async def test_validation_failed_toolmessage_does_not_count_as_submitted(self):
        history = [
            HumanMessage(content="audit"),
            AIMessage(
                content="", tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": [{}]}, "id": "c3"}]
            ),
            ToolMessage(content="Validation failed: ...", name=SUBMIT_FINDINGS_TOOL_NAME, tool_call_id="c3"),
        ]
        text_only = ModelResponse(result=[AIMessage(content="giving up, no findings")])
        handler = _handler_returning(text_only)

        await SubmitFindingsEnforcerMiddleware().awrap_model_call(_request(history), handler)

        assert len(handler.seen) == 1 + MAX_FINALIZE_NUDGES  # still nudged: nothing was recorded


class TestSubmissionPromptCopy:
    def test_tool_description_states_the_terminal_ordering(self):
        # Spec 6.1: the description is part of the effective prompt — it must carry both halves
        # of the ordering contract, not just "call once".
        description = SUBMIT_FINDINGS_DESCRIPTION.lower()
        assert "only tool call" in description
        assert "final tool call" in description
        assert "one-line acknowledgement" in description

    def test_finalize_nudge_lets_an_incomplete_detector_keep_auditing(self):
        # Spec 6.2 / acceptance criteria: the old nudge said "Call submit_findings now", which
        # told a half-finished detector to stop auditing and submit whatever it had.
        assert "Call `submit_findings` now" not in FINALIZE_NUDGE
        assert "continue the audit first" in FINALIZE_NUDGE
        assert "only tool call" in FINALIZE_NUDGE
        assert FINALIZE_NUDGE.startswith("<system-reminder>")  # stays ephemeral, never persisted

    def test_batch_rejection_never_reads_as_a_successful_submission(self):
        # The whole mechanism rests on this: the rejection must not carry the success marker,
        # or the enforcer and the deferred-output extractor would treat a batch as recorded.
        rendered = BATCHED_SUBMIT_REJECTION.format(siblings=1)
        assert not rendered.startswith(SUBMITTED_MARKER)
        assert SUBMITTED_MARKER not in rendered
        assert "ONLY tool call" in rendered


def _tool_request(tool_call: dict, messages: list) -> ToolCallRequest:
    return ToolCallRequest(tool_call=tool_call, tool=Mock(), state={"messages": messages}, runtime=Mock())


def _ai_with_calls(*names: str) -> tuple:
    """An AIMessage carrying one tool call per name, plus the tool_calls it issued."""
    calls = [{"name": name, "args": {}, "id": f"c{i}"} for i, name in enumerate(names)]
    return AIMessage(content="", tool_calls=calls), calls


class TestSubmitFindingsBatchRejection:
    async def _run(self, tool_call, messages):
        executed = []

        async def handler(request):
            executed.append(request)
            return ToolMessage(
                content=f"{SUBMITTED_MARKER} (0 finding(s)).",
                name=request.tool_call["name"],
                tool_call_id=request.tool_call["id"],
            )

        result = await SubmitFindingsEnforcerMiddleware().awrap_tool_call(_tool_request(tool_call, messages), handler)
        return result, executed

    async def test_sole_submit_call_executes_normally(self):
        message, calls = _ai_with_calls(SUBMIT_FINDINGS_TOOL_NAME)
        result, executed = await self._run(calls[0], [HumanMessage(content="audit"), message])

        assert len(executed) == 1
        assert result.content.startswith(SUBMITTED_MARKER)

    async def test_submit_batched_with_inspection_tool_is_rejected(self):
        # Spec 6.3: submission batched with a read is not a valid terminal submission. The read
        # still runs (it is a separate tool call); only the submission is refused.
        message, calls = _ai_with_calls("read_file", SUBMIT_FINDINGS_TOOL_NAME)
        submit_call = calls[1]
        result, executed = await self._run(submit_call, [message])

        assert executed == []  # the submit handler never ran -> nothing recorded
        assert not result.content.startswith(SUBMITTED_MARKER)
        assert "ONLY tool call" in result.content
        assert result.tool_call_id == submit_call["id"]
        assert result.name == SUBMIT_FINDINGS_TOOL_NAME

    async def test_two_submits_in_one_response_are_both_rejected(self):
        message, calls = _ai_with_calls(SUBMIT_FINDINGS_TOOL_NAME, SUBMIT_FINDINGS_TOOL_NAME)

        for call in calls:
            result, executed = await self._run(call, [message])
            assert executed == []
            assert not result.content.startswith(SUBMITTED_MARKER)

    async def test_other_tools_are_never_intercepted(self):
        message, calls = _ai_with_calls("read_file", "grep")
        result, executed = await self._run(calls[0], [message])

        assert len(executed) == 1  # a two-read batch is perfectly legal
        assert result.content.startswith(SUBMITTED_MARKER)  # handler stub's canned reply

    async def test_unknown_issuing_message_falls_through_to_the_handler(self):
        # Defensive: if the issuing AIMessage isn't in state (trimmed, summarized), a bookkeeping
        # miss must not swallow a legitimate submission — fail open and let it record.
        result, executed = await self._run(
            {"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {}, "id": "orphan"}, [HumanMessage(content="audit")]
        )

        assert len(executed) == 1
        assert result.content.startswith(SUBMITTED_MARKER)

    async def test_rejected_batch_still_triggers_the_finalize_nudge(self):
        # End-to-end of the two halves: a rejected batch leaves the run unsubmitted, so a later
        # text-only finish must still be nudged (invariant 7 — an unsuccessful attempt is not terminal).
        message, calls = _ai_with_calls("read_file", SUBMIT_FINDINGS_TOOL_NAME)
        history = [
            HumanMessage(content="audit"),
            message,
            ToolMessage(content="file contents", name="read_file", tool_call_id=calls[0]["id"]),
            ToolMessage(
                content=BATCHED_SUBMIT_REJECTION.format(siblings=1),
                name=SUBMIT_FINDINGS_TOOL_NAME,
                tool_call_id=calls[1]["id"],
            ),
        ]
        text_only = ModelResponse(result=[AIMessage(content="done, nothing to report")])
        handler = _handler_returning(text_only)

        await SubmitFindingsEnforcerMiddleware().awrap_model_call(_request(history), handler)

        assert len(handler.seen) == 1 + MAX_FINALIZE_NUDGES
