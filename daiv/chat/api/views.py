import logging
from typing import TYPE_CHECKING, Any, cast

from django.http import Http404, HttpRequest, StreamingHttpResponse

from ag_ui.core import RunAgentInput  # noqa: TC002
from ag_ui.encoder import EventEncoder
from copilotkit import LangGraphAGUIAgent
from langgraph.store.memory import InMemoryStore
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from automation.agent.graph import create_daiv_agent
from automation.agent.utils import build_langsmith_config
from chat.models import ChatThread
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from core.checkpointer import open_checkpointer
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


chat_router = Router(tags=["chat"], auth=[AuthBearer(), django_auth])
models_router = Router(auth=AuthBearer(), tags=["models"])


def _extract_first_user_message(input_data: RunAgentInput) -> str:
    return next((c for m in input_data.messages if isinstance(c := getattr(m, "content", ""), str) and c.strip()), "")


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
    """Handle one AG-UI run. Implicit-creates the ChatThread on first sight for the
    authenticated caller, enforces ownership thereafter, and rejects concurrent runs on
    the same thread.
    """
    repo_id = request.headers.get(HEADER_REPO_ID)
    ref = request.headers.get(HEADER_REF)
    if not repo_id or not ref:
        raise Http404("Repository ID or reference not found")

    user = request.auth  # ty: ignore[unresolved-attribute]  # attached by django-ninja
    thread_id = input_data.thread_id
    run_id = input_data.run_id

    thread, created = await ChatThread.objects.aget_or_create(
        thread_id=thread_id,
        defaults={"user": user, "repo_id": repo_id, "ref": ref, "title": _extract_first_user_message(input_data)[:120]},
    )
    if not created:
        if thread.user_id != user.id:
            raise HttpError(403, "Thread not found")
        if thread.active_run_id:
            raise HttpError(409, "A run is already in progress for this thread")

    thread.active_run_id = run_id
    await thread.asave(update_fields=["active_run_id", "last_active_at"])

    encoder = EventEncoder(accept=request.headers.get("accept"))

    async def event_generator():
        try:
            async with (
                open_checkpointer() as checkpointer,
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
        finally:
            thread.active_run_id = ""
            await thread.asave(update_fields=["active_run_id", "last_active_at"])

    return StreamingHttpResponse(event_generator(), content_type=encoder.get_content_type())
