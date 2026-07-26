import hashlib
import json
from unittest.mock import AsyncMock, Mock

from deepagents.backends.protocol import WriteResult
from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.middlewares.deferred_output import DeferredOutputMiddleware

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
    # Never drop output: returning None leaves `structured_response` set, so deepagents inlines
    # the payload instead of handing the orchestrator a pointer to a file that was never written.
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(error="disk full"))

    result = await _mw(backend).aafter_agent(
        {"structured_response": {"findings": []}, "messages": [AIMessage(content="done")]}, Mock()
    )

    assert result is None


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


async def test_a_promoted_submission_is_exported_as_json():
    # The seam between the two middlewares: `submit_findings` records findings by promoting them
    # into `structured_response`, and this middleware exports that channel. Nothing errors if the
    # two ever disagree on the key — langgraph silently drops an unknown `Command.update` key and
    # the detector would just look empty — so pin the producer against the consumer.
    from automation.agent.middlewares.submit_findings import _promote_submission

    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))
    findings = [{"detector": "performance", "line": 10}]
    promoted = _promote_submission(
        ToolMessage(
            content="Findings recorded (1).", name="submit_findings", tool_call_id="c1", artifact={"findings": findings}
        )
    )

    result = await _mw(backend).aafter_agent({**promoted.update, "messages": []}, Mock())

    payload = json.dumps({"findings": findings})
    backend.awrite.assert_awaited_once_with(f"{_OUTPUT_DIR}/cr-correctness-{_digest(payload)}.json", payload)
    assert result["structured_response"] is None


async def test_unsubmitted_run_falls_back_to_txt():
    # A detector that never recorded findings leaves no `structured_response`, so its final text
    # is deferred as `.txt` — which `findings.py merge` counts as a failed detector rather than a
    # clean one. A fabricated `.json` here would read downstream as "audited, nothing found".
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))

    await _mw(backend).aafter_agent({"messages": [AIMessage(content="gave up")]}, Mock())

    assert backend.awrite.await_args.args[0].endswith(".txt")
    assert backend.awrite.await_args.args[1] == "gave up"


async def test_trailing_empty_ai_message_does_not_erase_a_failed_detector():
    # Anthropic occasionally emits an empty `end_turn` AIMessage after a final tool call.
    # Reading messages[-1] literally would extract nothing, defer NO file at all, and make the
    # detector vanish from the fan-out — downstream that is indistinguishable from a clean run,
    # because `merge` only counts the files it is handed.
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))
    state = {"messages": [AIMessage(content="blocked: could not read the diff"), AIMessage(content="")]}

    result = await _mw(backend).aafter_agent(state, Mock())

    assert backend.awrite.await_args.args[0].endswith(".txt")
    assert backend.awrite.await_args.args[1] == "blocked: could not read the diff"
    assert result is not None


async def test_detector_with_no_extractable_text_still_defers_a_failure_sentinel():
    # The contract is that a failed detector always leaves a path for `merge` to count as
    # skipped. "No submission and no text" must not become "no file".
    backend = Mock()
    backend.awrite = AsyncMock(return_value=WriteResult(path="ok"))

    result = await _mw(backend).aafter_agent({"messages": [AIMessage(content="")]}, Mock())

    assert backend.awrite.await_args.args[0].endswith(".txt")
    assert "no structured output" in backend.awrite.await_args.args[1]
    assert result is not None
