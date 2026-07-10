import logging

from django.http import Http404, HttpRequest, StreamingHttpResponse

from ag_ui.core import RunAgentInput  # noqa: TC002
from ag_ui.encoder import EventEncoder
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth
from sandbox_envs.services import resolve_env_for_run, resolve_env_for_user
from sessions.locks import SessionLock
from sessions.models import Session

from automation.agent.validators import AgentOverrideError, ensure_agent_model_available, validate_agent_override
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, aassert_can_run
from core.api.throttling import JobsRateThrottle

from .security import AuthBearer
from .streaming import ChatRunStreamer
from .threads import ChatSessionService, _extract_first_user_message

logger = logging.getLogger("daiv.chat")

HEADER_REPO_ID = "X-Repo-ID"
HEADER_REF = "X-Ref"
HEADER_SANDBOX_ENV = "X-Sandbox-Env"

chat_router = Router(tags=["chat"], auth=[AuthBearer(), django_auth])


@chat_router.get("/threads/{thread_id}/status", response=dict, url_name="thread_status")
async def thread_status(request: HttpRequest, thread_id: str):
    """Cheap probe so a reloaded page can detect when its in-flight run has released
    the per-thread slot and trigger a rehydration from the checkpointer.
    """
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await Session.objects.by_owner(user).filter(thread_id=thread_id).afirst()
    if session is None:
        raise HttpError(404, "Thread not found")
    return {"active": bool(session.active_run_id)}


@chat_router.post(
    "/completions",
    response=dict,
    throttle=[JobsRateThrottle()],
    url_name="completions",
    openapi_extra={
        "parameters": [
            {"in": "header", "name": HEADER_REPO_ID, "schema": {"type": "string"}, "required": True},
            {"in": "header", "name": HEADER_REF, "schema": {"type": "string"}, "required": True},
            {"in": "header", "name": HEADER_SANDBOX_ENV, "schema": {"type": "string"}, "required": False},
        ]
    },
)
async def create_chat_completion(request: HttpRequest, input_data: RunAgentInput):
    """AG-UI streaming endpoint. First sight of a ``thread_id`` creates its ``Session``
    under the authenticated caller; subsequent requests must be able to see it (a
    webhook-origin session with ``user=None`` is continuable by anyone with visibility).
    ``SessionLock.try_claim`` atomically claims the per-session run slot — parallel tabs
    resolve to a single winner, the loser gets 409.
    """
    repo_id = request.headers.get(HEADER_REPO_ID)
    ref = request.headers.get(HEADER_REF)
    if not repo_id or not ref:
        raise Http404("Repository ID or reference not found")

    user = request.auth  # ty: ignore[unresolved-attribute]
    try:
        await aassert_can_run(user, [repo_id])
    except RepositoryAccessDenied as err:
        # Opaque 404: don't confirm the repo's existence to unauthorized callers.
        raise Http404(REPO_ACCESS_DENIED_MESSAGE) from err

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
    # Auto: snapshot the resolved env at session creation so the stored env matches what ran.
    # Existing sessions keep their original env (get_or_create_for_user only applies on create);
    # this resolution still runs on every request but is discarded for existing sessions.
    auto_resolved = env_obj is None
    if auto_resolved:
        env_obj = await resolve_env_for_run(user=user, repo_id=repo_id)
        logger.debug(
            "chat: auto-resolved env=%s for repo=%s user=%s", env_obj.id if env_obj else None, repo_id, user.pk
        )

    session, created = await ChatSessionService.get_or_create_for_user(
        user=user,
        thread_id=thread_id,
        repo_id=repo_id,
        ref=ref,
        input_data=input_data,
        sandbox_environment=env_obj,
        agent_model=agent_model,
        agent_thinking_level=agent_thinking_level,
    )
    # Ownership: an existing session must be visible to the caller. A webhook-origin
    # session (user=None) is visible to anyone who can see it, so "continue as chat"
    # is just typing into any visible session — this replaces the old
    # ``thread.user_id != user.id`` equality check and the ChatThreadFromActivity bridge.
    if not created and not await Session.objects.by_owner(user).filter(pk=session.pk).aexists():
        raise HttpError(403, "Thread not found")

    # Re-validate the pinned override before any other gate: a Provider row may
    # have been disabled or renamed since the session was created, OR a thinking
    # level enum value may have been dropped. Surface a typed 400 first so the
    # user gets the actionable "start a new thread" hint — even when they also
    # tried to send a divergent override, the pinned model is the blocker.
    # ``validate_agent_override`` is a no-op when both fields are empty, so the
    # call is unconditional.
    try:
        validate_agent_override(session.agent_model, session.agent_thinking_level)
    except AgentOverrideError as err:
        raise HttpError(
            400, f"The model pinned to this thread is no longer available: {err}. Start a new thread to pick another."
        ) from err

    # Submit-time gate: refuse the call when no model can be resolved at runtime.
    # On a freshly created session this catches "client omitted the override + admin
    # never set a system default"; on resume it catches sessions pinned to "" back
    # when the now-removed Auto fallback supplied the model. Either way, surface
    # the configuration gap here instead of letting it explode mid-stream.
    try:
        ensure_agent_model_available(session.agent_model)
    except AgentOverrideError as err:
        raise HttpError(400, str(err)) from err

    # First-turn pin: an existing session keeps the override that was set on creation.
    # If the client supplies a divergent override (e.g. a bot bypassing the locked
    # composer pill), reject with 409 rather than silently running the pinned value.
    # Empty client values mean "no override supplied" and never count as a divergence.
    if not created and (
        (agent_model and agent_model != session.agent_model)
        or (agent_thinking_level and agent_thinking_level != session.agent_thinking_level)
    ):
        raise HttpError(
            409,
            "Agent override is pinned for this thread; remove agent_model / agent_thinking_level"
            " from forwarded_props or start a new thread to change it.",
        )

    if not await SessionLock.try_claim(thread_id, run_id):
        raise HttpError(409, "A run is already in progress for this thread")

    # Only emit the resolved-env hint when:
    # - The client sent Auto (empty/missing header) AND we resolved something for them, AND
    # - This is a newly-created session (so the resolved env *is* what the run is using —
    #   on an existing-session Auto submit, the resolved env_obj is discarded in favour of
    #   the session's stored env, and lying about it would mis-stamp the locked pill).
    auto_resolved_env: dict[str, str] | None = None
    if auto_resolved and created and env_obj is not None:
        auto_resolved_env = {"id": str(env_obj.id), "name": str(env_obj.name), "scope": str(env_obj.scope)}

    encoder = EventEncoder(accept=request.headers.get("accept"))
    streamer = ChatRunStreamer(
        repo_id=repo_id,
        ref=ref,
        thread_id=thread_id,
        run_id=run_id,
        input_data=input_data,
        user_id=user.pk,
        prompt=_extract_first_user_message(input_data),
        sandbox_environment_id=(str(session.sandbox_environment_id) if session.sandbox_environment_id else None),
        agent_model=session.agent_model or None,
        agent_thinking_level=session.agent_thinking_level or None,
        auto_resolved_env=auto_resolved_env,
    )
    return StreamingHttpResponse(
        (encoder.encode(event) async for event in streamer.events()), content_type=encoder.get_content_type()
    )
