import json
import logging
import time

from django.http import Http404, HttpRequest, StreamingHttpResponse

from ag_ui.core import RunAgentInput  # noqa: TC002
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from sandbox_envs.services import resolve_env_for_run, resolve_env_for_user
from sessions.locks import SessionLock, stale_cutoff
from sessions.models import Session

from automation.agent.validators import AgentOverrideError, ensure_agent_model_available, validate_agent_override
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied, aassert_can_run
from core.api.throttling import JobsRateThrottle

from . import relay, runner
from .security import AuthBearer
from .streaming import ChatRunStreamer
from .threads import ChatSessionService, _extract_first_user_message, _extract_last_user_message_id

logger = logging.getLogger("daiv.chat")

HEADER_REPO_ID = "X-Repo-ID"
HEADER_REF = "X-Ref"
HEADER_SANDBOX_ENV = "X-Sandbox-Env"

chat_router = Router(tags=["chat"], auth=[AuthBearer(), django_auth])

# SSE reader tuning. The 300s duration cap closes the response *without* an end
# frame — EventSource then auto-reconnects with Last-Event-ID, which keeps
# long runs streaming while bounding per-connection worker occupancy.
STREAM_BLOCK_MS = 15_000
STREAM_DRAIN_BLOCK_MS = 500
STREAM_MAX_DURATION_S = 300.0


def _end_frame(reason: str) -> str:
    return f"event: end\ndata: {json.dumps({'reason': reason})}\n\n"


def _sse_response(frames) -> StreamingHttpResponse:
    """Wrap a relay-tail generator in the response every SSE endpoint shares.

    ``X-Accel-Buffering: no`` + ``Cache-Control: no-cache`` are the load-bearing
    headers that stop nginx from buffering the stream — keep them in one place so
    the two callers can't drift.
    """
    return StreamingHttpResponse(
        frames, content_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


async def _run_event_frames(thread_id: str, run_id: str, last_id: str):
    """Replay + live-tail a run's relay stream as SSE frames.

    Every data frame carries the Redis entry id as the SSE ``id:`` so browsers
    resume via ``Last-Event-ID``. Terminal ``event: end`` frames tell the client
    to stop reconnecting; hitting the duration cap closes silently on purpose
    (the browser reconnects and resumes).

    Liveness: when the stream goes quiet we probe the session slot. Holder
    released → drain the tail briefly (the sentinel may still be in flight,
    since ``events()`` releases the lock before the runner publishes it), then
    finish. Holder present but heartbeat-stale → the writer is dead; tell the
    client instead of hanging forever.

    A relay/DB error mid-tail emits an ``event: end`` with ``reason: "error"``
    rather than letting the generator raise: an unframed abort is indistinguishable
    from a transient drop to the browser's EventSource, which would then reconnect
    forever against a still-broken backend. The explicit terminal frame stops it.
    """
    yield "retry: 2000\n\n"
    start = time.monotonic()
    released_drain = False
    run_relay = relay.RunRelay(thread_id, run_id)

    try:
        while (time.monotonic() - start) < STREAM_MAX_DURATION_S:
            block_ms = STREAM_DRAIN_BLOCK_MS if released_drain else STREAM_BLOCK_MS
            entries = await run_relay.read_events(last_id, block_ms=block_ms)
            if entries:
                for entry in entries:
                    last_id = entry.id
                    if entry.is_end:
                        yield _end_frame("finished")
                        return
                    yield f"id: {entry.id}\ndata: {entry.data}\n\n"
                continue

            if released_drain:
                yield _end_frame("finished")
                return

            session = (
                await Session.objects.filter(thread_id=thread_id).values("active_run_id", "last_active_at").afirst()
            )
            if session is None or session["active_run_id"] != run_id:
                released_drain = True
                continue
            if session["last_active_at"] < stale_cutoff():
                yield _end_frame("stale")
                return
            yield ": keep-alive\n\n"
    except Exception:
        logger.exception("chat: relay tail failed for thread_id=%s run_id=%s", thread_id, run_id)
        yield _end_frame("error")


@chat_router.get("/stream", url_name="chat_run_stream")
async def stream_run_events(request: HttpRequest, thread_id: str, run_id: str):
    """Resumable SSE stream of a chat run's AG-UI events.

    Replays from the start (or from the ``Last-Event-ID`` header on browser
    reconnect) and tails live until the run's terminal sentinel. Authorization
    is thread visibility: the relay key embeds the thread id, so a run id from
    another thread reads an empty stream even if guessed.
    """
    user = request.auth  # ty: ignore[unresolved-attribute]
    if not await Session.objects.by_owner(user).filter(thread_id=thread_id).aexists():
        raise HttpError(404, "Thread not found")
    last_id = request.headers.get("Last-Event-ID") or "0-0"
    return _sse_response(_run_event_frames(thread_id, run_id, last_id))


class RunHandle(Schema):
    """The default ``POST /completions`` response: a pointer to the detached run.

    Every subsequent ``GET /stream`` / ``POST /cancel`` call is built from this
    pair, so it's a durable client contract (unlike the trivial probe dicts other
    endpoints return) and gets a schema of its own — mirroring ``CancelIn``.
    """

    run_id: str
    thread_id: str


@chat_router.post(
    "/completions",
    response=RunHandle,
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

    Runs execute detached (see ``chat.api.runner``); the response is a run handle (JSON) or an
    inline relay tail (``Accept: text/event-stream``).
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

    streamer = ChatRunStreamer(
        repo_id=repo_id,
        ref=ref,
        thread_id=thread_id,
        run_id=run_id,
        input_data=input_data,
        user_id=user.pk,
        prompt=_extract_first_user_message(input_data),
        message_id=_extract_last_user_message_id(input_data),
        sandbox_environment_id=(str(session.sandbox_environment_id) if session.sandbox_environment_id else None),
        agent_model=session.agent_model or None,
        agent_thinking_level=session.agent_thinking_level or None,
        auto_resolved_env=auto_resolved_env,
    )
    # The run is detached from this request: it executes as a background task
    # and publishes to the relay, so a client disconnect no longer kills it.
    runner.supervisor.spawn(streamer)

    if "text/event-stream" in (request.headers.get("accept") or ""):
        # AG-UI protocol compatibility: SSE callers get the same relay-backed
        # frames inline. Dropping this response only drops the tail (the detached
        # run keeps going). Note the 300s duration cap closes without an end frame:
        # a browser EventSource auto-reconnects with Last-Event-ID, but a raw AG-UI
        # client that doesn't must reconnect via ``GET /stream`` with a
        # ``Last-Event-ID`` header to resume a run longer than the cap.
        return _sse_response(_run_event_frames(thread_id, run_id, "0-0"))
    return {"run_id": run_id, "thread_id": thread_id}


class CancelIn(Schema):
    thread_id: str
    run_id: str


@chat_router.post("/cancel", response=dict, url_name="chat_run_cancel")
async def cancel_chat_run(request: HttpRequest, payload: CancelIn):
    """Explicitly stop an in-flight chat run.

    Required because disconnects no longer cancel runs: the Redis flag stops
    the run at its next event boundary wherever it executes; the local task
    cancel is immediate when the run lives in this process.
    """
    user = request.auth  # ty: ignore[unresolved-attribute]
    session = await Session.objects.by_owner(user).filter(thread_id=payload.thread_id).afirst()
    if session is None:
        raise HttpError(404, "Thread not found")
    if session.active_run_id != payload.run_id:
        raise HttpError(409, "Run is not in flight for this thread")

    await relay.RunRelay(payload.thread_id, payload.run_id).request_cancel()
    cancelled_locally = runner.supervisor.cancel_local(payload.run_id)
    return {"cancelled": True, "local": cancelled_locally}
