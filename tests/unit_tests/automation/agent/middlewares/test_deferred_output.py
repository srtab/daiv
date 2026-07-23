import hashlib
import json
from unittest.mock import AsyncMock, Mock

from deepagents.backends.protocol import WriteResult
from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.middlewares.deferred_output import DeferredOutputMiddleware
from automation.agent.middlewares.submit_findings import SUBMIT_FINDINGS_TOOL_NAME, SUBMITTED_MARKER

_OUTPUT_DIR = "/workspace/tmp/subagent-output"


def _mw(backend):
    return DeferredOutputMiddleware(backend=backend, name="cr-correctness", output_dir=_OUTPUT_DIR)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


async def test_structured_response_written_as_json_and_pointer_returned():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))
    structured = {"findings": [{"detector": "correctness", "line": 10}]}
    payload = json.dumps(structured)
    expected_path = f"{_OUTPUT_DIR}/cr-correctness-{_digest(payload)}.json"

    result = await _mw(backend).aafter_agent(
        {"structured_response": structured, "messages": [AIMessage(content="done")]}, Mock()
    )

    backend.awrite.assert_awaited_once_with(expected_path, payload)
    assert result["structured_response"] is None
    assert len(result["messages"]) == 1
    text = result["messages"][0].text
    assert expected_path in text
    assert "deferred to a file" in text


async def test_text_output_written_as_txt():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))
    expected_path = f"{_OUTPUT_DIR}/cr-correctness-{_digest('free text')}.txt"

    result = await _mw(backend).aafter_agent({"messages": [AIMessage(content="free text")]}, Mock())

    backend.awrite.assert_awaited_once_with(expected_path, "free text")
    assert result["structured_response"] is None
    text = result["messages"][0].text
    assert expected_path in text
    assert "deferred to a file" in text


async def test_write_failure_keeps_inline_output():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(error="disk full"))

    result = await _mw(backend).aafter_agent(
        {"structured_response": {"findings": []}, "messages": [AIMessage(content="done")]}, Mock()
    )

    assert result is None  # no state update -> structured_response survives -> deepagents inlines it


async def test_write_raises_keeps_inline_output():
    backend = Mock()
    backend.awrite = AsyncMock(side_effect=RuntimeError("boom"))

    result = await _mw(backend).aafter_agent(
        {"structured_response": {"findings": []}, "messages": [AIMessage(content="done")]}, Mock()
    )

    assert result is None


async def test_already_exists_is_treated_as_success():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(error="path already exists"))

    result = await _mw(backend).aafter_agent(
        {"structured_response": {"findings": []}, "messages": [AIMessage(content="done")]}, Mock()
    )

    assert result is not None
    assert result["structured_response"] is None


async def test_nothing_to_defer_returns_none():
    backend = Mock()
    backend.awrite = AsyncMock()

    result = await _mw(backend).aafter_agent({"messages": []}, Mock())

    assert result is None
    backend.awrite.assert_not_awaited()


async def test_serialize_failure_keeps_inline_output_and_skips_write():
    # The "never drop findings" contract has two halves: a backend write failure (covered above)
    # AND serialization itself raising. A structured_response that isn't JSON-serializable (a set)
    # makes json.dumps raise inside _extract; aafter_agent must swallow it, return None so
    # deepagents re-inlines structured_response, and never even attempt the write.
    backend = Mock()
    backend.awrite = AsyncMock()

    result = await _mw(backend).aafter_agent(
        {"structured_response": {"findings": {1, 2, 3}}, "messages": [AIMessage(content="done")]}, Mock()
    )

    assert result is None
    backend.awrite.assert_not_awaited()


def _submit_round_trip(findings: list, call_id: str = "call-1", ok: bool = True) -> list:
    return [
        AIMessage(
            content="", tool_calls=[{"name": SUBMIT_FINDINGS_TOOL_NAME, "args": {"findings": findings}, "id": call_id}]
        ),
        ToolMessage(
            content=(f"{SUBMITTED_MARKER} ({len(findings)} finding(s))." if ok else "Validation failed: nope"),
            name=SUBMIT_FINDINGS_TOOL_NAME,
            tool_call_id=call_id,
        ),
    ]


async def test_submitted_findings_extracted_from_tool_call_as_json():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))
    findings = [{"detector": "performance", "line": 10}]
    payload = json.dumps({"findings": findings})
    expected_path = f"{_OUTPUT_DIR}/cr-correctness-{_digest(payload)}.json"

    state = {"messages": [*_submit_round_trip(findings), AIMessage(content="Done: 1 finding.")]}
    result = await _mw(backend).aafter_agent(state, Mock())

    backend.awrite.assert_awaited_once_with(expected_path, payload)
    assert expected_path in result["messages"][0].text


async def test_submitted_payload_wins_over_trailing_text_and_structured_response():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))
    payload = json.dumps({"findings": []})

    state = {
        "messages": [*_submit_round_trip([]), AIMessage(content="prose that must NOT be exported")],
        "structured_response": {"findings": [{"detector": "stale"}]},
    }
    await _mw(backend).aafter_agent(state, Mock())

    written_payload = backend.awrite.await_args.args[1]
    assert written_payload == payload


async def test_validation_failed_submit_does_not_count_falls_back_to_text():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))

    state = {"messages": [*_submit_round_trip([{}], ok=False), AIMessage(content="gave up")]}
    await _mw(backend).aafter_agent(state, Mock())

    # Nothing was recorded → the .txt fallback path (failed detector), never a fabricated .json.
    written_path = backend.awrite.await_args.args[0]
    assert written_path.endswith(".txt")
    assert backend.awrite.await_args.args[1] == "gave up"


async def test_last_successful_submit_wins():
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))
    first = _submit_round_trip([{"detector": "old"}], call_id="c1")
    second = _submit_round_trip([{"detector": "new"}], call_id="c2")

    state = {"messages": [*first, *second, AIMessage(content="done")]}
    await _mw(backend).aafter_agent(state, Mock())

    assert json.loads(backend.awrite.await_args.args[1]) == {"findings": [{"detector": "new"}]}
