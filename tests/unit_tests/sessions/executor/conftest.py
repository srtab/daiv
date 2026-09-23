import uuid

from sessions.models import Session, SessionOrigin


async def make_session(**fields) -> str:
    """Create a job ``Session`` row and return its thread id."""
    thread_id = str(uuid.uuid4())
    await Session.objects.acreate(thread_id=thread_id, origin=SessionOrigin.API_JOB, repo_id="owner/repo", **fields)
    return thread_id


async def active_holder(thread_id: str) -> str | None:
    return (await Session.objects.aget(thread_id=thread_id)).active_run_id
