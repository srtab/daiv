import logging
from typing import TYPE_CHECKING, Any, cast

from django.conf import settings as django_settings
from django.http import Http404, HttpRequest, StreamingHttpResponse

from ag_ui.core import RunAgentInput  # noqa: TC002
from ag_ui.encoder import EventEncoder
from copilotkit import LangGraphAGUIAgent
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.store.memory import InMemoryStore
from ninja import Router

from automation.agent.graph import create_daiv_agent
from automation.agent.utils import build_langsmith_config
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from core.site_settings import site_settings

from .security import AuthBearer

if TYPE_CHECKING:
    from ag_ui.core.events import BaseEvent  # noqa: TC002

logger = logging.getLogger("daiv.chat")

HEADER_REPO_ID = "X-Repo-ID"
HEADER_REF = "X-Ref"


class RuntimeContextLangGraphAGUIAgent(LangGraphAGUIAgent):
    """
    Forward the daiv RuntimeCtx dataclass as LangGraph's typed `context=` kwarg.

    Upstream's `get_stream_kwargs` only accepts dict-shaped contexts (it merges via
    `dict.update`), but our graph declares `context_schema=RuntimeCtx` and expects the
    frozen dataclass itself.
    """

    def __init__(self, *, runtime_context: Any, **kwargs: Any):
        super().__init__(**kwargs)
        self._runtime_context = runtime_context

    def get_stream_kwargs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        stream_kwargs = super().get_stream_kwargs(*args, **kwargs)
        stream_kwargs.setdefault("context", self._runtime_context)
        return stream_kwargs


chat_router = Router(tags=["chat"])
models_router = Router(auth=AuthBearer(), tags=["models"])


@chat_router.post(
    "/completions",
    response=dict,
    openapi_extra={
        "parameters": [
            {"in": "header", "name": HEADER_REPO_ID, "schema": {"type": "string"}, "required": True},
            {"in": "header", "name": HEADER_REF, "schema": {"type": "string"}, "required": True},
        ]
    },
)
async def create_chat_completion(request: HttpRequest, input_data: RunAgentInput):
    """
    This endpoint is used to create a chat completion for a given set of messages within the indexed codebase.

    The main goal is to have an OpenAI compatible API to allow seamless integration with existing tools and services.
    """
    repo_id = request.headers.get(HEADER_REPO_ID)
    ref = request.headers.get(HEADER_REF)

    if not repo_id or not ref:
        raise Http404("Repository ID or reference not found")

    encoder = EventEncoder(accept=request.headers.get("accept"))

    async def event_generator():
        async with (
            AsyncRedisSaver.from_conn_string(
                django_settings.DJANGO_REDIS_CHECKPOINT_URL,
                ttl={"default_ttl": django_settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES},
            ) as checkpointer,
            set_runtime_ctx(repo_id=repo_id, scope=Scope.GLOBAL, ref=ref) as runtime_ctx,
        ):
            agent = await create_daiv_agent(ctx=runtime_ctx, checkpointer=checkpointer, store=InMemoryStore())
            langsmith_config = build_langsmith_config(
                runtime_ctx,
                trigger="chat",
                model=site_settings.agent_model_name,
                thinking_level=site_settings.agent_thinking_level,
            )
            langgraph_agent = RuntimeContextLangGraphAGUIAgent(
                name="DAIV",
                description="DAIV agent",
                graph=agent,
                config={"recursion_limit": 500, **langsmith_config},
                runtime_context=runtime_ctx,
            )
            async for event in langgraph_agent.run(input_data):
                yield encoder.encode(cast("BaseEvent", event))

    return StreamingHttpResponse(event_generator(), content_type=encoder.get_content_type())
