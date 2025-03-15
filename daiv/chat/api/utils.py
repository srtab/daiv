from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from automation.tools.repository import SEARCH_CODE_SNIPPETS_NAME
from chat.api.schemas import ChatCompletionChunk

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langchain_core.runnables.schema import StreamEvent

    from automation.agents.codebase_chat import CodebaseChatAgent

logger = logging.getLogger("daiv.chat")


def extract_text_from_event_data(event_data: StreamEvent) -> str:
    if isinstance(event_data["data"]["chunk"].content, list) and len(event_data["data"]["chunk"].content) > 0:
        if event_data["data"]["chunk"].content[0]["type"] == "text":
            return event_data["data"]["chunk"].content[0]["text"]
    elif isinstance(event_data["data"]["chunk"].content, str):
        return event_data["data"]["chunk"].content
    return ""


def format_tool_output(event_data: StreamEvent) -> str:
    """
    Format the result of a tool call.
    """
    if event_data["name"] == SEARCH_CODE_SNIPPETS_NAME:
        if "output" not in event_data["data"]:
            query = event_data["data"]["input"]["query"]
            intent = event_data["data"]["input"]["intent"]
            return f"\n\n---\n\n### 🔍 Searching repositories:\n * Query: `{query}`\n * Intent: {intent}\n\n"
        else:
            pattern = r"<CodeSnippet\s+([^>]+)>(.*?)</CodeSnippet>"
            snippets = []
            for attr_str, snippet_content in re.findall(pattern, event_data["data"]["output"].content, re.DOTALL):
                attributes = dict(re.findall(r'(\w+)="([^"]*)"', attr_str))
                snippets.append(
                    "🧩 Relevant snippet found in: "
                    f"[`{attributes['repository']}/{attributes['path']}`]({attributes['external_link']})\n"
                    f"````\n{snippet_content.strip()}\n````\n\n"
                )
            return "".join(snippets)
    return ""


async def generate_stream(codebase_chat: CodebaseChatAgent, input_data: dict, model_id: str, config: RunnableConfig):
    """
    Generate a stream of chat completion events.
    """
    chunk_uuid = str(uuid.uuid4())
    created = int(datetime.now().timestamp())

    try:
        async for event_data in codebase_chat.agent.astream_events(input_data, version="v2", config=config):
            if (
                event_data["event"] in ("on_tool_start", "on_tool_end")
                and event_data["metadata"]["langgraph_node"] == "tools"
            ):
                chat_chunk = ChatCompletionChunk(
                    id=chunk_uuid,
                    created=created,
                    model=model_id,
                    choices=[
                        {
                            "index": 0,
                            "finish_reason": None,
                            "delta": {"content": format_tool_output(event_data), "role": "assistant"},
                        }
                    ],
                )
                yield f"data: {chat_chunk.model_dump_json()}\n\n"
            elif event_data["event"] == "on_chat_model_stream" and event_data["metadata"]["langgraph_node"] == "agent":
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

    except Exception as e:
        logger.exception("Error generating stream.")
        chat_chunk = ChatCompletionChunk(
            id=chunk_uuid,
            created=created,
            model=model_id,
            choices=[{"index": 0, "finish_reason": "stop", "delta": {"content": "", "role": "assistant"}}],
        )
        yield f"data: {chat_chunk.model_dump_json()}\n\n"
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
