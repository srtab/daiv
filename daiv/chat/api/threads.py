from typing import TYPE_CHECKING

from django.utils import timezone

from chat.models import ChatThread

if TYPE_CHECKING:
    from ag_ui.core import RunAgentInput

    from accounts.models import User
    from codebase.base import MergeRequest


def _extract_first_user_message(input_data: RunAgentInput) -> str:
    return next((c for m in input_data.messages if isinstance(c := getattr(m, "content", ""), str) and c.strip()), "")


class ChatThreadService:
    """Encapsulates ``ChatThread`` row operations needed by the chat API.

    The view stays out of the model directly — every read/write goes through
    this service so the per-thread run-slot protocol (``aget_or_create`` →
    conditional ``UPDATE`` claim → ``UPDATE`` release) lives in one place.
    """

    @staticmethod
    async def get_or_create_for_user(
        *, user: User, thread_id: str, repo_id: str, ref: str, input_data: RunAgentInput
    ) -> ChatThread:
        """First sight of ``thread_id`` creates the row under ``user``; later calls
        return the existing row regardless of owner. Caller must enforce ownership.
        """
        thread, _created = await ChatThread.objects.aget_or_create(
            thread_id=thread_id,
            defaults={
                "user": user,
                "repo_id": repo_id,
                "ref": ref,
                "title": _extract_first_user_message(input_data)[:120],
            },
        )
        return thread

    @staticmethod
    async def try_claim_run(thread_id: str, run_id: str) -> bool:
        """Atomic claim: only succeeds if the slot is currently free. Avoids TOCTOU
        between a "is it free?" read and a "claim it" write when two tabs fire
        simultaneously.
        """
        claimed = await ChatThread.objects.filter(thread_id=thread_id, active_run_id="").aupdate(
            active_run_id=run_id, last_active_at=timezone.now()
        )
        return bool(claimed)

    @staticmethod
    async def release_run(thread_id: str) -> None:
        await ChatThread.objects.filter(thread_id=thread_id).aupdate(active_run_id="", last_active_at=timezone.now())

    @staticmethod
    async def persist_ref(thread_id: str, original_ref: str, mr: MergeRequest | None) -> None:
        """Sync ``ChatThread.ref`` with the agent's final ``merge_request`` (captured
        from the live STATE_SNAPSHOT stream — no second checkpoint read needed).
        """
        new_ref = mr.source_branch if mr else None
        if new_ref and new_ref != original_ref:
            await ChatThread.objects.filter(thread_id=thread_id).aupdate(ref=new_ref)
