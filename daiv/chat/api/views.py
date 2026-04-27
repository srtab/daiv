from django.http import Http404, HttpRequest, StreamingHttpResponse

from ag_ui.core import RunAgentInput  # noqa: TC002
from ag_ui.encoder import EventEncoder
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from chat.models import ChatThread

from .security import AuthBearer
from .streaming import ChatRunStreamer
from .threads import ChatThreadService

HEADER_REPO_ID = "X-Repo-ID"
HEADER_REF = "X-Ref"

chat_router = Router(tags=["chat"], auth=[AuthBearer(), django_auth])
models_router = Router(auth=AuthBearer(), tags=["models"])


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

    thread = await ChatThreadService.get_or_create_for_user(
        user=user, thread_id=thread_id, repo_id=repo_id, ref=ref, input_data=input_data
    )
    if thread.user_id != user.id:
        raise HttpError(403, "Thread not found")

    if not await ChatThreadService.try_claim_run(thread_id, run_id):
        raise HttpError(409, "A run is already in progress for this thread")

    encoder = EventEncoder(accept=request.headers.get("accept"))
    streamer = ChatRunStreamer(
        repo_id=repo_id, ref=ref, thread_id=thread_id, run_id=run_id, input_data=input_data, encoder=encoder
    )
    return StreamingHttpResponse(streamer.events(), content_type=encoder.get_content_type())
