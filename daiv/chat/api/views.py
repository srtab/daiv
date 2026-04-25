import logging
from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, Any, cast

from django.http import Http404, HttpRequest, StreamingHttpResponse
from django.utils import timezone

from ag_ui.core import RunAgentInput  # noqa: TC002
from ag_ui.core.events import CustomEvent, EventType, RunErrorEvent
from ag_ui.encoder import EventEncoder
from copilotkit import LangGraphAGUIAgent
from langgraph.store.memory import InMemoryStore
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from automation.agent.graph import create_daiv_agent
from automation.agent.utils import build_langsmith_config
from chat.models import ChatThread
from chat.repo_state import CUSTOM_EVENT_NAME as REPO_STATE_EVENT
from chat.repo_state import aget_existing_mr_payload, mr_to_payload
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

    def get_schema_keys(self, config: Any) -> dict[str, list[str]]:
        # Upstream calls ``graph.config_schema().schema()`` which recurses into
        # ``context_schema=RuntimeCtx``. RuntimeCtx holds a ``git.Repo`` field that pydantic
        # cannot turn into JSON schema, so the call raises PydanticInvalidForJsonSchema.
        # Derive context keys from the dataclass directly and keep the rest of the shape
        # matching upstream's contract.
        ctx_schema = getattr(self.graph, "context_schema", None)
        context_keys = [f.name for f in fields(ctx_schema)] if is_dataclass(ctx_schema) else []
        constant = list(self.constant_schema_keys)
        return {"input": constant, "output": constant, "config": [], "context": context_keys}


chat_router = Router(tags=["chat"], auth=[AuthBearer(), django_auth])
models_router = Router(auth=AuthBearer(), tags=["models"])


def _extract_first_user_message(input_data: RunAgentInput) -> str:
    return next((c for m in input_data.messages if isinstance(c := getattr(m, "content", ""), str) and c.strip()), "")


async def _release_thread(thread_id: str) -> None:
    await ChatThread.objects.filter(thread_id=thread_id).aupdate(active_run_id="", last_active_at=timezone.now())


async def _emit_repo_state(agent: Any, thread_id: str, repo_id: str, original_ref: str):
    """Yield a CustomEvent carrying the post-run repo state (current ref + MR).

    Best-effort: if state lookup blows up we log and swallow — a missing pill
    update is not worth aborting the response.
    """
    try:
        state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        logger.exception("chat: failed to read final agent state for repo-state event")
        return

    state_values = (getattr(state, "values", None) or {}) if state else {}
    mr_payload = mr_to_payload(state_values.get("merge_request"))
    new_ref = mr_payload["source_branch"] if mr_payload and mr_payload.get("source_branch") else original_ref

    # If the agent didn't touch an MR, surface any pre-existing one for this branch
    # so the composer pill appears even when the run was a no-op on git state.
    if mr_payload is None:
        mr_payload = await aget_existing_mr_payload(repo_id, new_ref)

    if new_ref == original_ref and mr_payload is None:
        return

    if new_ref != original_ref:
        await ChatThread.objects.filter(thread_id=thread_id).aupdate(ref=new_ref)

    yield CustomEvent(type=EventType.CUSTOM, name=REPO_STATE_EVENT, value={"ref": new_ref, "merge_request": mr_payload})


@chat_router.get("/threads/{thread_id}/status", response=dict)
async def thread_status(request: HttpRequest, thread_id: str):
    """Cheap probe so a reloaded page can detect when its in-flight run has released
    the per-thread slot and trigger a rehydration from the checkpointer.
    """
    user = request.auth  # ty: ignore[unresolved-attribute]
    thread = await ChatThread.objects.filter(thread_id=thread_id, user=user).afirst()
    if thread is None:
        raise HttpError(404, "Thread not found")
    return {"active": bool(thread.active_run_id)}


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
    """AG-UI streaming endpoint. First sight of a ``thread_id`` creates its ``ChatThread``
    under the authenticated caller; subsequent requests must own it. Uses a conditional
    ``UPDATE`` on ``active_run_id`` to atomically claim the per-thread run slot — races
    between parallel tabs resolve to a single winner and a 409 for the loser.
    """
    repo_id = request.headers.get(HEADER_REPO_ID)
    ref = request.headers.get(HEADER_REF)
    if not repo_id or not ref:
        raise Http404("Repository ID or reference not found")

    user = request.auth  # ty: ignore[unresolved-attribute]  # populated by AuthBearer/django_auth
    thread_id = input_data.thread_id
    run_id = input_data.run_id

    thread, _created = await ChatThread.objects.aget_or_create(
        thread_id=thread_id,
        defaults={"user": user, "repo_id": repo_id, "ref": ref, "title": _extract_first_user_message(input_data)[:120]},
    )
    if thread.user_id != user.id:
        raise HttpError(403, "Thread not found")

    # Atomic claim: only succeeds if the slot is currently free. Avoids TOCTOU between a
    # "is it free?" read and a "claim it" write when two tabs fire simultaneously.
    claimed = await ChatThread.objects.filter(thread_id=thread_id, active_run_id="").aupdate(
        active_run_id=run_id, last_active_at=timezone.now()
    )
    if not claimed:
        raise HttpError(409, "A run is already in progress for this thread")

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

                # End-of-run repo-state probe: GitMiddleware may have committed
                # changes to a *different* branch than the one we started on,
                # and the merge_request it stashed in state is private — it
                # never makes it into the AG-UI STATE_SNAPSHOT stream. Read
                # it back out of the checkpoint and surface it as a CUSTOM
                # event so the composer pills can update without a reload.
                async for repo_event in _emit_repo_state(agent, thread_id, repo_id, ref):
                    yield encoder.encode(repo_event)
        except Exception as exc:
            logger.exception("Chat run failed for thread_id=%s run_id=%s", thread_id, run_id)
            yield encoder.encode(
                RunErrorEvent(type=EventType.RUN_ERROR, message=f"{type(exc).__name__}: {exc}", code="run_failed")
            )
        finally:
            await _release_thread(thread_id)

    return StreamingHttpResponse(event_generator(), content_type=encoder.get_content_type())
