import hashlib
import json
from unittest.mock import AsyncMock, Mock

from deepagents.backends.protocol import WriteResult
from langchain_core.messages import AIMessage

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
