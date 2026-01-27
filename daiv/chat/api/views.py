import logging
import uuid
from datetime import datetime

from django.http import Http404, HttpRequest, StreamingHttpResponse

from langchain_core.runnables import RunnableConfig
from ninja import Router

from automation.agent.graph import create_daiv_agent
from automation.agent.utils import extract_text_content
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from core.constants import BOT_NAME

from .schemas import ChatCompletionRequest, ChatCompletionResponse, ModelListSchema, ModelSchema
from .security import AuthBearer
from .utils import generate_stream

logger = logging.getLogger("daiv.chat")

MODEL_ID = "DAIV"
HEADER_REPO_ID = "X-Repo-ID"
HEADER_REF = "X-Ref"


chat_router = Router(auth=AuthBearer(), tags=["chat"])
models_router = Router(auth=AuthBearer(), tags=["models"])


@chat_router.post(
    "/completions",
    response=ChatCompletionResponse | dict,
    openapi_extra={
        "parameters": [
            {"in": "header", "name": HEADER_REPO_ID, "schema": {"type": "string"}, "required": True},
            {"in": "header", "name": HEADER_REF, "schema": {"type": "string"}, "required": True},
        ]
    },
)
async def create_chat_completion(request: HttpRequest, payload: ChatCompletionRequest):
    """
    This endpoint is used to create a chat completion for a given set of messages within the indexed codebase.

    The main goal is to have an OpenAI compatible API to allow seamless integration with existing tools and services.
    """
    repo_id, ref = request.headers.get(HEADER_REPO_ID), request.headers.get(HEADER_REF)

    input_data = {"messages": [msg.dict() for msg in payload.messages]}
    config = RunnableConfig(
        metadata={"model_id": MODEL_ID, "chat_stream": payload.stream, "repo_id": repo_id, "ref": ref}
    )

    if payload.stream:
        return StreamingHttpResponse(
            generate_stream(input_data, MODEL_ID, repo_id=repo_id, ref=ref, config=config),
            content_type="text/event-stream",
        )
    try:
        async with set_runtime_ctx(repo_id=repo_id, scope=Scope.GLOBAL, ref=ref) as runtime_ctx:
            daiv_agent = await create_daiv_agent(ctx=runtime_ctx)
            result = await daiv_agent.ainvoke(input_data, config=config, context=runtime_ctx)

        return ChatCompletionResponse(
            id=str(uuid.uuid4()),
            created=int(datetime.now().timestamp()),
            choices=[
                {
                    "index": 1,
                    "message": {
                        "content": extract_text_content(result["messages"][-1].content),
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
async def get_models(request: HttpRequest):
    """
    This endpoint is used to get the list of models available for the chat completion.
    """
    return ModelListSchema(object="list", data=[await get_model(request, MODEL_ID)])


@models_router.get("/{model_id}", response={200: ModelSchema})
async def get_model(request: HttpRequest, model_id: str):
    """
    This endpoint is used to get the model information.
    """
    if model_id != MODEL_ID:
        raise Http404("Model not found")
    return ModelSchema(id=MODEL_ID, object="model", created=None, owned_by=BOT_NAME)
