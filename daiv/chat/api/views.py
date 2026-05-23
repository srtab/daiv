import logging

from django.http import Http404, HttpRequest, StreamingHttpResponse

from ag_ui.core import RunAgentInput  # noqa: TC002
from ag_ui.encoder import EventEncoder
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth
from sandbox_envs.services import resolve_env_for_run, resolve_env_for_user

from automation.agent.validators import AgentOverrideError, validate_agent_override
from chat.models import ChatThread
from core.api.throttling import JobsRateThrottle

from .security import AuthBearer
from .streaming import ChatRunStreamer
from .threads import ChatThreadService

logger = logging.getLogger("daiv.chat")

HEADER_REPO_ID = "X-Repo-ID"
HEADER_REF = "X-Ref"
HEADER_SANDBOX_ENV = "X-Sandbox-Env"

chat_router = Router(tags=["chat"], auth=[AuthBearer(), django_auth])


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
    throttle=[JobsRateThrottle()],
    openapi_extra={
        "parameters": [
            {"in": "header", "name": HEADER_REPO_ID, "schema": {"type": "string"}, "required": True},
            {"in": "header", "name": HEADER_REF, "schema": {"type": "string"}, "required": True},
            {"in": "header", "name": HEADER_SANDBOX_ENV, "schema": {"type": "string"}, "required": False},
        ]
    },
)
async def create_chat_completion(request: HttpRequest, input_data: RunAgentInput):
    """AG-UI streaming endpoint. First sight of a ``thread_id`` creates its ``ChatThread``
    under the authenticated caller; subsequent requests must own it. The conditional
    ``UPDATE`` on ``active_run_id`` atomically claims the per-thread run slot —
    parallel tabs resolve to a single winner, the loser gets 409.
    """
    repo_id = request.headers.get(HEADER_REPO_ID)
    ref = request.headers.get(HEADER_REF)
    if not repo_id or not ref:
        raise Http404("Repository ID or reference not found")

    user = request.auth  # ty: ignore[unresolved-attribute]
    thread_id = input_data.thread_id
    run_id = input_data.run_id

    forwarded = getattr(input_data, "forwarded_props", None) or {}
    try:
        agent_model, agent_thinking_level = validate_agent_override(
            forwarded.get("agent_model"), forwarded.get("agent_thinking_level")
        )
    except AgentOverrideError as err:
        raise HttpError(400, str(err)) from err

    env_header = request.headers.get(HEADER_SANDBOX_ENV)
    try:
        env_obj = await resolve_env_for_user(user, env_header)
    except LookupError as err:
        raise HttpError(400, str(err)) from err
    # Auto: snapshot the resolved env at thread creation so the stored env matches what ran.
    # Existing threads keep their original env (get_or_create_for_user only applies on create);
    # this resolution still runs on every request but is discarded for existing threads.
    if env_obj is None:
        env_obj = await resolve_env_for_run(user=user, repo_id=repo_id)
        logger.debug(
            "chat: auto-resolved env=%s for repo=%s user=%s", env_obj.id if env_obj else None, repo_id, user.pk
        )

    thread = await ChatThreadService.get_or_create_for_user(
        user=user,
        thread_id=thread_id,
        repo_id=repo_id,
        ref=ref,
        input_data=input_data,
        sandbox_environment=env_obj,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
    )
    if thread.user_id != user.id:
        raise HttpError(403, "Thread not found")

    if not await ChatThreadService.try_claim_run(thread_id, run_id):
        raise HttpError(409, "A run is already in progress for this thread")

    encoder = EventEncoder(accept=request.headers.get("accept"))
    streamer = ChatRunStreamer(
        repo_id=repo_id,
        ref=ref,
        thread_id=thread_id,
        run_id=run_id,
        input_data=input_data,
        encoder=encoder,
        sandbox_environment_id=(str(thread.sandbox_environment_id) if thread.sandbox_environment_id else None),
        agent_model=thread.agent_model or None,
        agent_thinking_level=thread.agent_thinking_level or None,
    )
    return StreamingHttpResponse(streamer.events(), content_type=encoder.get_content_type())
