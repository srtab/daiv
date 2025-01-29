import logging
import uuid
from datetime import datetime

from django.http import Http404, HttpRequest, StreamingHttpResponse

from ninja import Router

from automation.agents.codebase_qa.agent import CodebaseQAAgent
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from core.constants import BOT_NAME

from .schemas import ChatCompletionRequest, ChatCompletionResponse, ModelListSchema, ModelSchema
from .security import AsyncAuthBearer, AuthBearer
from .utils import format_references, generate_stream

logger = logging.getLogger("daiv.chat")

MODEL_ID = "DAIV"


chat_router = Router(auth=AsyncAuthBearer(), tags=["chat"])
models_router = Router(auth=AuthBearer(), tags=["models"])


@chat_router.post("/completions", response=ChatCompletionResponse | dict)
async def create_chat_completion(request: HttpRequest, payload: ChatCompletionRequest):
    """
    This endpoint is used to create a chat completion for a given set of messages within the indexed codebase.

    The main goal is to have an OpenAI compatible API to allow seamless integration with existing tools and services.
    """
    input_data = {"messages": [msg.dict() for msg in payload.messages]}

    codebase_qa = CodebaseQAAgent(index=CodebaseIndex(RepoClient.create_instance()))

    if payload.stream:
        return StreamingHttpResponse(
            generate_stream(codebase_qa, input_data, MODEL_ID), content_type="text/event-stream"
        )
    try:
        result = codebase_qa.agent.invoke(input_data)

        return ChatCompletionResponse(
            id=str(uuid.uuid4()),
            created=int(datetime.now().timestamp()),
            choices=[
                {
                    "index": 1,
                    "message": {
                        "content": result.content + format_references(result.references),
                        "role": "assistant",
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
        )
    except Exception as e:
        return {"error": str(e)}


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
