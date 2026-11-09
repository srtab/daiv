from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from deepagents.middleware.filesystem import _is_read_file_media_result
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import ToolMessage

from automation.agent.events import TOOL_MEDIA_EVENT
from automation.agent.tool_media import (
    DAIV_MEDIA_KEY,
    FAILED,
    TOO_LARGE,
    UNSUPPORTED_TYPE,
    ToolMediaError,
    ToolMediaStore,
    media_blocks,
    result_messages,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command

logger = logging.getLogger("daiv.tools")


class ToolMediaMiddleware(AgentMiddleware):
    """Keeps the media blocks of the main agent's tool results so the chat transcript can show them.

    The model's result is never changed. Each block's outcome goes in ``additional_kwargs[DAIV_MEDIA_KEY]`` for the
    reload path, and in a ``TOOL_MEDIA_EVENT`` for the live stream, whose result event fires before this wrapper
    returns (``on_tool_end`` runs inside the tool call) and so can't carry it.
    """

    def __init__(self, *, store: ToolMediaStore) -> None:
        self._store = store

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        result = await handler(request)
        thread_id = _thread_id(request)
        for message, blocks in _media_by_tool_message(result):
            entries = [
                await self._entry(block, ordinal, thread_id=thread_id, tool_call_id=message.tool_call_id)
                for ordinal, block in enumerate(blocks)
            ]
            message.additional_kwargs[DAIV_MEDIA_KEY] = entries
            await _dispatch({"thread_id": thread_id, "tool_call_id": message.tool_call_id, "media": entries})
        return result

    async def _entry(self, block: dict[str, Any], ordinal: int, *, thread_id: str, tool_call_id: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"ordinal": ordinal, "kind": block["type"], "mime_type": block.get("mime_type") or ""}
        data = block.get("base64")
        url = block.get("url")
        if data is None and isinstance(url, str) and url.startswith("data:"):
            header, _, data = url.partition(",")
            if not header.endswith(";base64"):
                return {**entry, "error": UNSUPPORTED_TYPE}
            entry["mime_type"] = header.removeprefix("data:").removesuffix(";base64") or entry["mime_type"]
        elif data is None:
            if url:
                return {**entry, "url": url}
            return {**entry, "error": UNSUPPORTED_TYPE}
        try:
            if not isinstance(data, str):
                raise TypeError(type(data).__name__)
            encoded = "".join(data.split())
            if (size := len(encoded) * 3 // 4 - encoded[-2:].count("=")) > self._store.max_bytes:
                return {**entry, "size": size, "error": TOO_LARGE}
            content = base64.b64decode(encoded, validate=True)
        except ValueError, TypeError:
            logger.warning("Media %d of tool call %s is not valid base64; not keeping it", ordinal, tool_call_id)
            return {**entry, "error": FAILED}
        entry["size"] = len(content)
        if not thread_id:
            logger.warning("Media %d of tool call %s has no thread to keep it on", ordinal, tool_call_id)
            return {**entry, "error": FAILED}
        try:
            media_id = await self._store.astore(
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                ordinal=ordinal,
                mime_type=entry["mime_type"],
                content=content,
            )
        except ToolMediaError as exc:
            return {**entry, "error": exc.code}
        except Exception:
            logger.exception("Could not keep media %d of tool call %s", ordinal, tool_call_id)
            return {**entry, "error": FAILED}
        return {**entry, "id": media_id}


def _thread_id(request: ToolCallRequest) -> str:
    config = getattr(request.runtime, "config", None) or {}
    return (config.get("configurable") or {}).get("thread_id") or ""


def _media_by_tool_message(result: ToolMessage | Command) -> list[tuple[ToolMessage, list[dict[str, Any]]]]:
    """Each tool message of ``result`` that has media, with its blocks followed by those of the frames message
    deepagents' video ``read_file`` returns beside it."""
    messages = result_messages(result)
    frames = {
        message.additional_kwargs.get("read_file_tool_call_id"): media_blocks(message)
        for message in messages
        if _is_read_file_media_result(message)
    }
    return [
        (message, blocks)
        for message in messages
        if isinstance(message, ToolMessage)
        and (blocks := [*media_blocks(message), *frames.get(message.tool_call_id, [])])
    ]


async def _dispatch(payload: dict[str, Any]) -> None:
    try:
        await adispatch_custom_event(TOOL_MEDIA_EVENT, payload)
    except Exception:
        logger.warning("Could not dispatch tool media; the live transcript will not show it", exc_info=True)
