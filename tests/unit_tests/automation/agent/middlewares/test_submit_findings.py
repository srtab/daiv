from unittest.mock import Mock

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse, ToolCallRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from automation.agent.middlewares.submit_findings import (
    BATCHED_SUBMIT_REJECTION,
    FINALIZE_NUDGE,
    MAX_FINALIZE_NUDGES,
    SUBMIT_FINDINGS_DESCRIPTION,
    SUBMIT_FINDINGS_TOOL_NAME,
    SubmitFindingsEnforcerMiddleware,
    build_submit_findings_tool,
)
from tests.unit_tests.automation.agent.fakes import FakeToolModel

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


def _submit(tool, findings: list, call_id: str = "call-1") -> ToolMessage:
    """Invoke the tool the way the graph does — as a tool call, so the artifact materializes."""
    return tool.invoke({
        "name": SUBMIT_FINDINGS_TOOL_NAME,
        "args": {"findings": findings},
        "id": call_id,
        "type": "tool_call",
    })


class TestBuildSubmitFindingsTool:
    def test_tool_identity_and_schema(self):
        tool = build_submit_findings_tool(_SCHEMA)
        assert tool.name == SUBMIT_FINDINGS_TOOL_NAME
        # The model must see the full findings schema as the tool's args — that is how it
        # learns the finding shape now that response_format is gone.
        assert tool.args_schema == _SCHEMA

    def test_valid_payload_returns_the_findings_as_the_artifact(self):
        # The artifact is both the success signal and the payload the enforcer promotes into
        # state; a success that carried only prose would record nothing.
        tool = build_submit_findings_tool(_SCHEMA)
        findings = [{"detector": "performance"}, {"detector": "performance"}]

        result = _submit(tool, findings)

        assert result.artifact == {"findings": findings}
        assert "2 finding(s)" in result.content

    def test_empty_findings_list_is_a_valid_submission(self):
        # A clean audit is a submission, not a non-submission: it must carry an artifact so the
        # run terminates instead of being nudged for a result it already gave.
        tool = build_submit_findings_tool(_SCHEMA)

        result = _submit(tool, [])

        assert result.artifact == {"findings": []}
        assert "0 finding(s)" in result.content

    def test_invalid_payload_returns_validation_error_and_no_artifact(self):
        tool = build_submit_findings_tool(_SCHEMA)

        result = _submit(tool, [{"unexpected": True}])

        assert result.artifact is None  # nothing recorded -> the run stays alive and retryable
        assert "Validation failed" in result.content
        assert SUBMIT_FINDINGS_TOOL_NAME in result.content  # tells the model to retry the same tool

    def test_real_detector_schema_accepts_empty_findings(self):
        # Pin the integration with the real skill schema: the wrapped object schema from
        # subagents.py must at minimum accept the empty submission.
        from automation.agent.subagents import _load_detector_findings_schema

        tool = build_submit_findings_tool(_load_detector_findings_schema())
        assert _submit(tool, []).artifact == {"findings": []}

    def test_custom_rules_finding_without_source_is_rejected_not_acknowledged(self):
        # `findings.py merge` drops a sourceless custom-rules finding. Acknowledging it here
        # would tell the model "recorded", export it as a success-signalling .json, and then
        # discard it at merge with no chance to correct — a silent loss of a real finding.
        tool = build_submit_findings_tool(_SCHEMA)

        result = _submit(tool, [{"detector": "custom-rules"}])

        assert result.artifact is None
        assert "Validation failed" in result.content
        assert "`source`" in result.content

    def test_the_submit_gate_and_the_merge_gate_agree_on_custom_rules_source(self):
        # Two validators for one type is how drift starts; pin that they accept the same thing.
        import importlib.util

        from daiv.settings.components import PROJECT_DIR

        path = PROJECT_DIR / "automation" / "agent" / "skills" / "code-review" / "scripts" / "findings.py"
        spec = importlib.util.spec_from_file_location("daiv_findings_for_submit_test", path)
        findings_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(findings_mod)

        from automation.agent.subagents import _load_detector_findings_schema

        tool = build_submit_findings_tool(_load_detector_findings_schema())
        sourced = {
            "detector": "custom-rules",
            "file": "a.py",
            "line": 1,
            "bar": "defect",
            "archetype": "remove_dead_lines",
            "title": "t",
            "rationale": "r",
            "suggestion": "s",
            "source": "AGENTS.md:3 — rule",
        }
        sourceless = {k: v for k, v in sourced.items() if k != "source"}

        assert _submit(tool, [sourced]).artifact is not None
        assert findings_mod.is_valid(sourced)
        assert _submit(tool, [sourceless]).artifact is None
        assert not findings_mod.is_valid(sourceless)


def _request(messages: list, recorded: dict | None = None) -> ModelRequest:
    """A model request whose state carries `recorded` as an already-promoted submission."""
    state = {"messages": messages}
    if recorded is not None:
        state["structured_response"] = recorded
    return ModelRequest(model=GenericFakeChatModel(messages=iter([])), messages=messages, state=state)


def _submit_call_history() -> list:
    """History where a successful submit_findings round-trip already happened."""
    return [
        HumanMessage(content="audit this"),
        AIMessage(
            content="", tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": []}, "id": "call-1"}]
        ),
        ToolMessage(content="Findings recorded (0 finding(s)).", name=SUBMIT_FINDINGS_TOOL_NAME, tool_call_id="call-1"),
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

    async def test_recorded_submission_ends_the_run_without_calling_the_model(self):
        # The terminus, and the anti-runaway property: once findings are recorded the model is
        # never invoked again, so it cannot resume inspecting (the pattern-locked failure mode)
        # and the detector does not pay a full-context call just to say "done".
        handler = _handler_returning(ModelResponse(result=[AIMessage(content="should never be produced")]))

        result = await SubmitFindingsEnforcerMiddleware().awrap_model_call(
            _request(_submit_call_history(), recorded={"findings": []}), handler
        )

        assert handler.seen == []
        assert isinstance(result, AIMessage)
        assert not result.tool_calls  # tool-call-free -> the graph routes to the exit chain

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

    async def test_failed_submit_attempt_does_not_terminate_the_run(self):
        # A validation failure leaves nothing promoted, so the model keeps its turn and gets
        # nudged — this is why the terminus cannot be `return_direct`, which fires on any call.
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


def _tool_call_names(messages: list) -> list[str]:
    return [call["name"] for m in messages if isinstance(m, AIMessage) for call in m.tool_calls or []]


class TestDetectorRunTerminus:
    """The submission→state→terminus path against a real compiled graph.

    Every step here fails *silently* if it drifts: langgraph drops a `Command.update` key that is
    not a declared channel, a state key the model node cannot see just reads as "not submitted",
    and a terminus that does not fire simply lets the run continue. None of those raise, and the
    hand-built states in the unit tests above cannot catch any of them.
    """

    @staticmethod
    def _agent(*scripted: AIMessage):
        """An agent whose model replays `scripted` — one message per model call."""
        return create_agent(
            model=FakeToolModel(messages=iter(scripted)),
            tools=[build_submit_findings_tool(_SCHEMA)],
            system_prompt="audit",
            middleware=[SubmitFindingsEnforcerMiddleware()],
        )

    async def test_a_successful_submission_ends_the_run_and_lands_in_state(self):
        findings = [{"detector": "correctness"}]
        agent = self._agent(
            AIMessage(
                content="", tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": findings}, "id": "s1"}]
            ),
            # Scripted but unreachable: the model is only asked again if the terminus fails, so a
            # `read_file` in the transcript is proof the detector bought another turn.
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "x"}, "id": "r1"}]),
        )

        result = await agent.ainvoke({"messages": [HumanMessage(content="audit this")]})

        assert _tool_call_names(result["messages"]) == [SUBMIT_FINDINGS_TOOL_NAME]
        # DeferredOutputMiddleware exports exactly this channel — see the seam test in
        # test_deferred_output.py.
        assert result["structured_response"] == {"findings": findings}
        assert not result["messages"][-1].tool_calls

    async def test_a_rejected_payload_leaves_the_run_alive_to_retry(self):
        # The mirror image: an invalid payload must NOT terminate, or the correct-and-retry loop
        # is gone and one bad field costs the whole detector. The second submission is only
        # reachable if the run survived the first, so its payload landing in state is the proof.
        good = [{"detector": "correctness"}]
        agent = self._agent(
            AIMessage(
                content="",
                tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": [{"bad": 1}]}, "id": "s1"}],
            ),
            AIMessage(
                content="", tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": good}, "id": "s2"}]
            ),
        )

        result = await agent.ainvoke({"messages": [HumanMessage(content="audit this")]})

        assert result["structured_response"] == {"findings": good}


class TestSubmissionPromptCopy:
    def test_tool_description_states_the_terminal_ordering(self):
        # Spec 6.1: the description is part of the effective prompt — it must carry both halves
        # of the ordering contract, not just "call once".
        description = SUBMIT_FINDINGS_DESCRIPTION.lower()
        assert "only tool call" in description
        assert "final tool call" in description
        # The run really does end at a successful call, so the description has to say so — a model
        # that expects a turn afterwards would defer inspection it will never get to do.
        assert "ends the run immediately" in description

    def test_finalize_nudge_lets_an_incomplete_detector_keep_auditing(self):
        # Spec 6.2 / acceptance criteria: the old nudge said "Call submit_findings now", which
        # told a half-finished detector to stop auditing and submit whatever it had.
        assert "Call `submit_findings` now" not in FINALIZE_NUDGE
        assert "continue the audit first" in FINALIZE_NUDGE
        assert "only tool call" in FINALIZE_NUDGE
        assert FINALIZE_NUDGE.startswith("<system-reminder>")  # stays ephemeral, never persisted

    def test_batch_rejection_tells_the_model_how_to_recover(self):
        # The rejection is the model's only feedback channel here, so it must name the fix rather
        # than just refuse — and must not read as "stop auditing".
        rendered = BATCHED_SUBMIT_REJECTION.format(siblings=1)
        assert "ONLY tool call" in rendered
        assert "continue the audit" in rendered


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
                content="Findings recorded (0 finding(s)).",
                name=request.tool_call["name"],
                tool_call_id=request.tool_call["id"],
                artifact={"findings": []},
            )

        result = await SubmitFindingsEnforcerMiddleware().awrap_tool_call(_tool_request(tool_call, messages), handler)
        return result, executed

    async def test_sole_submit_call_is_executed_and_promoted_into_state(self):
        message, calls = _ai_with_calls(SUBMIT_FINDINGS_TOOL_NAME)
        result, executed = await self._run(calls[0], [HumanMessage(content="audit"), message])

        assert len(executed) == 1
        # `structured_response` is the exact channel DeferredOutputMiddleware exports, and an
        # unknown key would be dropped by langgraph without an error, so assert the key itself.
        assert isinstance(result, Command)
        assert result.update["structured_response"] == {"findings": []}
        # The tool result rides along: langgraph rejects a tool Command with no matching ToolMessage.
        assert [m.tool_call_id for m in result.update["messages"]] == [calls[0]["id"]]

    async def test_submit_batched_with_inspection_tool_is_rejected(self):
        # Spec 6.3: submission batched with a read is not a valid terminal submission. The read
        # still runs (it is a separate tool call); only the submission is refused.
        message, calls = _ai_with_calls("read_file", SUBMIT_FINDINGS_TOOL_NAME)
        submit_call = calls[1]
        result, executed = await self._run(submit_call, [message])

        assert executed == []  # the submit handler never ran -> nothing recorded
        assert not isinstance(result, Command)  # nothing promoted -> the run stays alive
        assert "ONLY tool call" in result.content
        assert result.tool_call_id == submit_call["id"]
        assert result.name == SUBMIT_FINDINGS_TOOL_NAME

    async def test_two_submits_in_one_response_are_both_rejected(self):
        message, calls = _ai_with_calls(SUBMIT_FINDINGS_TOOL_NAME, SUBMIT_FINDINGS_TOOL_NAME)

        for call in calls:
            result, executed = await self._run(call, [message])
            assert executed == []
            assert not isinstance(result, Command)

    async def test_other_tools_are_never_intercepted_nor_promoted(self):
        # Promotion is scoped to submit_findings: another tool's artifact must never be mistaken
        # for a submission and end the run.
        message, calls = _ai_with_calls("read_file", "grep")
        result, executed = await self._run(calls[0], [message])

        assert len(executed) == 1  # a two-read batch is perfectly legal
        assert not isinstance(result, Command)

    async def test_unknown_issuing_message_falls_through_to_the_handler(self):
        # Defensive: if the issuing AIMessage isn't in state (trimmed, summarized), a bookkeeping
        # miss must not swallow a legitimate submission — fail open and let it record.
        result, executed = await self._run(
            {"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {}, "id": "orphan"}, [HumanMessage(content="audit")]
        )

        assert len(executed) == 1
        assert isinstance(result, Command)  # failing open still records

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
