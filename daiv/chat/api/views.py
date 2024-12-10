import json
import uuid
from datetime import datetime

from django.http import HttpRequest, StreamingHttpResponse

from ninja import Router

from automation.agents.codebase_qa.agent import CodebaseQAAgent
from chat.api.schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

router = Router()


@router.post("/chat/completions", response=ChatCompletionResponse | dict)
async def create_chat_completion(request: HttpRequest, payload: ChatCompletionRequest):
    messages = [msg.dict() for msg in payload.messages]

    codebase_qa = CodebaseQAAgent(index=CodebaseIndex(RepoClient.create_instance()))

    # Non-streaming completion
    if not payload.stream:
        try:
            result = codebase_qa.agent.invoke({"messages": messages})
            return ChatCompletionResponse(
                id=str(uuid.uuid4()),
                created=int(datetime.now().timestamp()),
                choices=[{"index": 1, "message": result["messages"][-1].content, "finish_reason": "stop"}],
            )
        except Exception as e:
            return {"error": str(e)}

    # Streaming completion
    async def generate_stream():
        try:
            async for chunk in codebase_qa.agent.astream({"messages": messages}, stream_mode="messages"):
                if chunk and chunk[0] and chunk[0].type == "AIMessageChunk":
                    content = None
                    if isinstance(chunk[0].content, str):
                        content = chunk[0].content
                    elif (
                        isinstance(chunk[0].content, list)
                        and chunk[0].content
                        and chunk[0].content[0]["type"] == "text"
                    ):
                        content = chunk[0].content[0]["text"]

                    chat_chunk = ChatCompletionChunk(
                        id=str(uuid.uuid4()),
                        created=int(datetime.now().timestamp()),
                        choices=[{"index": 1, "delta": {"content": content, "role": "ai"}}],
                    )
                    yield f"data: {chat_chunk.model_dump_json()}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingHttpResponse(generate_stream(), content_type="text/event-stream")


@router.get("/models")
def get_models(request: HttpRequest):
    return {
        "object": "list",
        "data": [{"id": "dipcode-gpt", "object": "model", "created": 1727404800, "owned_by": "dipcode"}],
    }
