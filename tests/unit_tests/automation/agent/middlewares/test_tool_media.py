import base64
import copy
import json
import logging
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from automation.agent.events import TOOL_MEDIA_EVENT
from automation.agent.middlewares.tool_media import ToolMediaMiddleware
from automation.agent.tool_media import RUN_LIMIT, ToolMediaError
from tests.unit_tests.conftest import FakeToolMediaStore

RAW = b"\x89PNG-bytes"
PNG = base64.b64encode(RAW).decode()


def _image(data: str = PNG, mime: str = "image/png") -> dict:
    return {"type": "image", "base64": data, "mime_type": mime}


def _request(config: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "shot", "args": {}, "id": "call_1"},
        tool=None,
        state={"messages": []},
        runtime=Mock(config=config if config is not None else {"configurable": {"thread_id": "t1"}}),
    )


def _returns(result):
    async def handler(request):
        return result

    return handler


def _tagged(message: ToolMessage) -> list[dict]:
    return message.additional_kwargs["daiv_media"]


@pytest.fixture
def dispatch():
    with patch("automation.agent.middlewares.tool_media.adispatch_custom_event", new_callable=AsyncMock) as mock:
        yield mock


async def test_stores_each_media_block_and_tags_it_by_ordinal(dispatch):
    message = ToolMessage(content=[{"type": "text", "text": "shot"}, _image(), _image()], tool_call_id="call_1")
    content = copy.deepcopy(message.content)
    store = FakeToolMediaStore()

    result = await ToolMediaMiddleware(store=store).awrap_tool_call(_request(), _returns(message))

    assert result.content == content
    assert _tagged(result) == [
        {"ordinal": 0, "kind": "image", "mime_type": "image/png", "id": str(uuid.UUID(int=1)), "size": len(RAW)},
        {"ordinal": 1, "kind": "image", "mime_type": "image/png", "id": str(uuid.UUID(int=2)), "size": len(RAW)},
    ]
    assert store.stored[0] == {
        "thread_id": "t1",
        "tool_call_id": "call_1",
        "ordinal": 0,
        "mime_type": "image/png",
        "content": RAW,
    }
    dispatch.assert_awaited_once_with(
        TOOL_MEDIA_EVENT, {"thread_id": "t1", "tool_call_id": "call_1", "media": _tagged(result)}
    )


async def test_store_error_is_recorded_and_the_result_survives(dispatch):
    message = ToolMessage(content=[_image()], tool_call_id="call_1")
    content = copy.deepcopy(message.content)

    result = await ToolMediaMiddleware(store=FakeToolMediaStore(error=ToolMediaError(RUN_LIMIT))).awrap_tool_call(
        _request(), _returns(message)
    )

    assert result.content == content
    assert _tagged(result) == [
        {"ordinal": 0, "kind": "image", "mime_type": "image/png", "size": len(RAW), "error": "run_limit"}
    ]


async def test_store_crash_records_failed_and_logs(dispatch, caplog):
    message = ToolMessage(content=[_image()], tool_call_id="call_1")

    with caplog.at_level(logging.WARNING, logger="daiv.tools"):
        result = await ToolMediaMiddleware(store=FakeToolMediaStore(error=RuntimeError("disk"))).awrap_tool_call(
            _request(), _returns(message)
        )

    assert _tagged(result)[0]["error"] == "failed"
    assert any(r.name == "daiv.tools" for r in caplog.records)


async def test_invalid_base64_records_failed_and_other_blocks_still_store(dispatch):
    message = ToolMessage(content=[_image("!!notbase64!!"), _image()], tool_call_id="call_1")
    store = FakeToolMediaStore()

    result = await ToolMediaMiddleware(store=store).awrap_tool_call(_request(), _returns(message))

    assert [entry.get("error") for entry in _tagged(result)] == ["failed", None]
    assert [s["ordinal"] for s in store.stored] == [1]


async def test_url_block_is_recorded_not_stored(dispatch):
    block = {"type": "image", "url": "https://x.test/a.png", "mime_type": "image/png"}
    store = FakeToolMediaStore()

    result = await ToolMediaMiddleware(store=store).awrap_tool_call(
        _request(), _returns(ToolMessage(content=[block], tool_call_id="call_1"))
    )

    assert _tagged(result) == [{"ordinal": 0, "kind": "image", "mime_type": "image/png", "url": "https://x.test/a.png"}]
    assert store.stored == []


async def test_block_without_bytes_or_url_is_unsupported(dispatch):
    block = {"type": "file", "file_id": "f1", "mime_type": "application/pdf"}

    result = await ToolMediaMiddleware(store=FakeToolMediaStore()).awrap_tool_call(
        _request(), _returns(ToolMessage(content=[block], tool_call_id="call_1"))
    )

    assert _tagged(result) == [
        {"ordinal": 0, "kind": "file", "mime_type": "application/pdf", "error": "unsupported_type"}
    ]


async def test_text_only_result_is_left_alone(dispatch):
    result = await ToolMediaMiddleware(store=FakeToolMediaStore()).awrap_tool_call(
        _request(), _returns(ToolMessage(content="ok", tool_call_id="call_1"))
    )

    assert "daiv_media" not in result.additional_kwargs
    dispatch.assert_not_awaited()


async def test_command_result_tags_its_tool_message(dispatch):
    message = ToolMessage(content=[_image()], tool_call_id="call_1")
    command = Command(update={"messages": [message]})

    result = await ToolMediaMiddleware(store=FakeToolMediaStore()).awrap_tool_call(_request(), _returns(command))

    assert result is command
    assert _tagged(message)[0]["id"] == str(uuid.UUID(int=1))


async def test_no_thread_id_records_failed_without_storing(dispatch):
    store = FakeToolMediaStore()

    result = await ToolMediaMiddleware(store=store).awrap_tool_call(
        _request(config={}), _returns(ToolMessage(content=[_image()], tool_call_id="call_1"))
    )

    assert _tagged(result)[0]["error"] == "failed"
    assert store.stored == []


async def test_video_frames_beside_the_tool_message_are_tagged_on_it(dispatch):
    message = ToolMessage(content="Read video a.mp4: sampled 1 frame.", tool_call_id="call_1", name="read_file")
    frames = HumanMessage(
        content=[{"type": "text", "text": "t=0s"}, _image(mime="image/jpeg")],
        additional_kwargs={"read_file_media_result": True, "read_file_tool_call_id": "call_1"},
    )
    store = FakeToolMediaStore()

    await ToolMediaMiddleware(store=store).awrap_tool_call(
        _request(), _returns(Command(update={"messages": [message, frames]}))
    )

    assert [entry["mime_type"] for entry in _tagged(message)] == ["image/jpeg"]
    assert store.stored[0]["content"] == RAW


async def test_an_oversized_block_is_rejected_before_decoding(dispatch):
    store = FakeToolMediaStore(max_bytes=len(RAW) - 1)

    result = await ToolMediaMiddleware(store=store).awrap_tool_call(
        _request(), _returns(ToolMessage(content=[_image()], tool_call_id="call_1"))
    )

    assert _tagged(result) == [
        {"ordinal": 0, "kind": "image", "mime_type": "image/png", "size": len(RAW), "error": "too_large"}
    ]
    assert store.stored == []


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(ChatAnthropic(model="claude-sonnet-5-5", api_key="x"), id="anthropic"),
        pytest.param(ChatOpenAI(model="gpt-5", api_key="x"), id="openai-chat"),
        pytest.param(ChatOpenAI(model="gpt-5", api_key="x", use_responses_api=True), id="openai-responses"),
    ],
)
def test_the_tag_never_reaches_the_provider(model):
    messages = [
        HumanMessage("hi"),
        AIMessage("", tool_calls=[{"name": "shot", "args": {}, "id": "call_1"}]),
        ToolMessage(
            content=[_image()],
            tool_call_id="call_1",
            additional_kwargs={"daiv_media": [{"ordinal": 0, "id": "SENTINEL"}]},
        ),
    ]

    payload = json.dumps(model._get_request_payload(messages), default=str)

    assert "call_1" in payload
    assert "SENTINEL" not in payload


async def test_a_non_string_payload_records_failed(dispatch):
    block = {"type": "image", "base64": 123, "mime_type": "image/png"}

    result = await ToolMediaMiddleware(store=FakeToolMediaStore()).awrap_tool_call(
        _request(), _returns(ToolMessage(content=[block], tool_call_id="call_1"))
    )

    assert _tagged(result)[0]["error"] == "failed"


async def test_mime_wrapped_base64_is_stored(dispatch):
    wrapped = "\n".join(PNG[i : i + 4] for i in range(0, len(PNG), 4))
    store = FakeToolMediaStore()

    await ToolMediaMiddleware(store=store).awrap_tool_call(
        _request(), _returns(ToolMessage(content=[_image(wrapped)], tool_call_id="call_1"))
    )

    assert store.stored[0]["content"] == RAW


async def test_a_data_url_block_is_stored_as_bytes(dispatch):
    block = {"type": "image", "url": f"data:image/png;base64,{PNG}"}
    store = FakeToolMediaStore()

    result = await ToolMediaMiddleware(store=store).awrap_tool_call(
        _request(), _returns(ToolMessage(content=[block], tool_call_id="call_1"))
    )

    assert _tagged(result) == [
        {"ordinal": 0, "kind": "image", "mime_type": "image/png", "id": str(uuid.UUID(int=1)), "size": len(RAW)}
    ]
    assert (store.stored[0]["mime_type"], store.stored[0]["content"]) == ("image/png", RAW)
