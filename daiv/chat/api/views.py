import json
import uuid
from datetime import datetime

from django.http import Http404, HttpRequest, StreamingHttpResponse

from ninja import Router

from automation.agents.codebase_qa.agent import CodebaseQAAgent
from chat.api.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelListSchema,
    ModelSchema,
)
from chat.api.security import AsyncAuthBearer, AuthBearer
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from core.constants import BOT_NAME

MODEL_ID = "DAIV"


chat_router = Router(auth=AsyncAuthBearer(), tags=["chat"])
models_router = Router(auth=AuthBearer(), tags=["models"])


def _extract_chunk_content(chunk):
    content = ""
    if isinstance(chunk.content, str):
        content = chunk.content
    elif isinstance(chunk.content, list) and chunk.content and chunk.content[0]["type"] == "text":
        content = chunk.content[0]["text"]
    return content


@chat_router.post("/completions", response=ChatCompletionResponse | dict, auth=AsyncAuthBearer())
async def create_chat_completion(request: HttpRequest, payload: ChatCompletionRequest):
    """
    This endpoint is used to create a chat completion for a given set of messages within the indexed codebase.

    The main goal is to have an OpenAI compatible API to allow seamless integration with existing tools and services.
    """
    messages = [msg.dict() for msg in payload.messages]

    codebase_qa = CodebaseQAAgent(index=CodebaseIndex(RepoClient.create_instance()))

    # Non-streaming completion
    if not payload.stream:
        try:
            result = codebase_qa.agent.invoke({"messages": messages})
            return ChatCompletionResponse(
                id=str(uuid.uuid4()),
                created=int(datetime.now().timestamp()),
                choices=[
                    {
                        "index": 1,
                        "message": {"content": result["messages"][-1].content, "role": "assistant", "tool_calls": []},
                        "finish_reason": "stop",
                    }
                ],
            )
        except Exception as e:
            return {"error": str(e)}

    # Streaming completion
    async def generate_stream():
        try:
            chunk_uuid = str(uuid.uuid4())
            created = int(datetime.now().timestamp())
            async for stream_event in codebase_qa.agent.astream_events({"messages": messages}, version="v2"):
                if (
                    stream_event["event"] == "on_chat_model_stream"
                    # the node query_or_respond can respond directly to the user too, so we need to stream it too
                    and stream_event["metadata"].get("langgraph_node") in ("query_or_respond", "generate")
                    and (chunk := stream_event["data"].get("chunk"))
                    # tool calls are handled in a different way, so we need to skip them
                    and not chunk.tool_call_chunks
                ):
                    chat_chunk = ChatCompletionChunk(
                        id=chunk_uuid,
                        created=created,
                        model=MODEL_ID,
                        choices=[
                            {
                                "index": 0,
                                "finish_reason": None,
                                "delta": {"content": _extract_chunk_content(chunk), "role": "assistant"},
                            }
                        ],
                    )
                    yield f"data: {chat_chunk.model_dump_json()}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingHttpResponse(generate_stream(), content_type="text/event-stream")


@models_router.get("", response={200: ModelListSchema})
def get_models(request: HttpRequest):
    """
    This endpoint is used to get the list of models available for the chat completion.
    """
    return ModelListSchema(object="list", data=[get_model(request, MODEL_ID)])


@models_router.get("/{model_id}", response={200: ModelSchema})
def get_model(request: HttpRequest, model_id: str):
    """
    This endpoint is used to get the model information.
    """
    if model_id != MODEL_ID:
        raise Http404("Model not found")
    return ModelSchema(id=MODEL_ID, object="model", created=None, owned_by=BOT_NAME)
