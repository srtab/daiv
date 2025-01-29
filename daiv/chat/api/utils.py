import json
import logging
import textwrap
import uuid
from datetime import datetime

from langchain_core.prompts.string import jinja2_formatter
from pydantic import HttpUrl

from automation.agents.codebase_qa import CodebaseQAAgent, FinalAnswer
from chat.api.schemas import ChatCompletionChunk
from chat.conf import settings

logger = logging.getLogger("daiv.chat")


async def generate_stream(codebase_qa: CodebaseQAAgent, input_data: dict, model_id: str):
    """
    Generate a stream of chat completion events.
    """
    chunk_uuid = str(uuid.uuid4())
    created = int(datetime.now().timestamp())
    is_reasoning = True

    try:
        if settings.REASONING:
            chat_chunk = ChatCompletionChunk(
                id=chunk_uuid,
                created=created,
                model=model_id,
                choices=[{"index": 0, "finish_reason": None, "delta": {"content": "<reason>\n", "role": "assistant"}}],
            )
            yield f"data: {chat_chunk.model_dump_json()}\n\n"

        previous_content = ""

        async for event_data in codebase_qa.agent.astream_events(
            input_data, version="v2", include_names=["CodebaseQAAgent"]
        ):
            if (
                event_data["event"] == "on_chain_stream"
                and event_data["data"]["chunk"]
                and event_data["data"]["chunk"].content
                and event_data["data"]["chunk"].content != previous_content
            ):
                if settings.REASONING and is_reasoning:
                    is_reasoning = False
                    chat_chunk = ChatCompletionChunk(
                        id=chunk_uuid,
                        created=created,
                        model=model_id,
                        choices=[
                            {
                                "index": 0,
                                "finish_reason": None,
                                "delta": {"content": "</reason>\n", "role": "assistant"},
                            }
                        ],
                    )
                    yield f"data: {chat_chunk.model_dump_json()}\n\n"

                chat_chunk = ChatCompletionChunk(
                    id=chunk_uuid,
                    created=created,
                    model=model_id,
                    choices=[
                        {
                            "index": 0,
                            "finish_reason": None,
                            "delta": {
                                # on_chain_stream event sends the full content, so we need to remove the previous
                                # content to get the delta
                                "content": event_data["data"]["chunk"].content.replace(previous_content, ""),
                                "role": "assistant",
                            },
                        }
                    ],
                )
                yield f"data: {chat_chunk.model_dump_json()}\n\n"

                previous_content = event_data["data"]["chunk"].content

            if (
                event_data["event"] == "on_chain_end"
                and event_data["data"]["output"]
                and isinstance(event_data["data"]["output"], FinalAnswer)
                and event_data["data"]["output"].references
            ):
                chat_chunk = ChatCompletionChunk(
                    id=chunk_uuid,
                    created=created,
                    model=model_id,
                    choices=[
                        {
                            "index": 0,
                            "finish_reason": None,
                            "delta": {
                                "content": format_references(event_data["data"]["output"].references),
                                "role": "assistant",
                            },
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


def format_references(references: list[HttpUrl]) -> str:
    return jinja2_formatter(
        textwrap.dedent(
            """\


            ---
            **References:**
            {% for reference in references %}
            - [{{ reference }}]({{ reference }})
            {% endfor %}
            """
        ),
        references=references,
    )
