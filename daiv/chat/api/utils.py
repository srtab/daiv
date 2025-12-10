from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from automation.agents.codebase_chat.agent import CodebaseChatAgent
from chat.api.schemas import ChatCompletionChunk
from codebase.context import set_runtime_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from langchain_core.runnables import RunnableConfig
    from langchain_core.runnables.schema import StreamEvent


logger = logging.getLogger("daiv.chat")


def extract_text_from_event_data(event_data: StreamEvent) -> str:
    """
    Extract the text from the event data.

    Args:
        event_data: The event data.

    Returns:
        The extracted text.
    """
    if isinstance(event_data["data"]["chunk"].content, list) and len(event_data["data"]["chunk"].content) > 0:
        if event_data["data"]["chunk"].content[0]["type"] == "text":
            return event_data["data"]["chunk"].content[0]["text"]
    elif isinstance(event_data["data"]["chunk"].content, str):
        return event_data["data"]["chunk"].content
    return ""


async def generate_stream(
    input_data: dict, model_id: str, *, repo_id: str, ref: str, config: RunnableConfig
) -> AsyncGenerator[str]:
    """
    Generate a stream of chat completion events.

    Args:
        codebase_chat: The codebase chat agent.
        input_data: The input data.
        model_id: The model ID.
        config: The config.

    Returns:
        The stream of chat completion events.
    """
    chunk_uuid = str(uuid.uuid4())
    created = int(datetime.now().timestamp())

    async with set_runtime_ctx(repo_id=repo_id, ref=ref) as runtime_ctx:
        try:
            # Get model config from repository config if available
            model_config = runtime_ctx.config.models.codebase_chat
            codebase_chat = await CodebaseChatAgent.get_runnable(
                model=model_config.model, temperature=model_config.temperature
            )

            async for event_data in codebase_chat.astream_events(input_data, config=config, context=runtime_ctx):
                if (
                    event_data["event"] == "on_chat_model_stream"
                    and event_data["metadata"]["langgraph_node"] == "model"
                ):
                    chat_chunk = ChatCompletionChunk(
                        id=chunk_uuid,
                        created=created,
                        model=model_id,
                        choices=[
                            {
                                "index": 0,
                                "finish_reason": None,
                                "delta": {"content": extract_text_from_event_data(event_data), "role": "assistant"},
                            }
                        ],
                    )
                    yield f"data: {chat_chunk.model_dump_json()}\n\n"

            chat_chunk = ChatCompletionChunk(
                id=chunk_uuid,
                created=created,
                model=model_id,
                choices=[{"index": 0, "finish_reason": "stop", "delta": {"content": "", "role": "assistant"}}],
            )
            yield f"data: {chat_chunk.model_dump_json()}\n\n"

        except Exception:
            logger.exception("Error generating stream.")
            chat_chunk = ChatCompletionChunk(
                id=chunk_uuid,
                created=created,
                model=model_id,
                choices=[{"index": 0, "finish_reason": "stop", "delta": {"content": "", "role": "assistant"}}],
            )
            yield f"data: {chat_chunk.model_dump_json()}\n\n"
            yield f"data: {json.dumps({'error': 'An internal error has occurred.'})}\n\n"
