from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from chat.models import ChatThread

if TYPE_CHECKING:
    from ag_ui.core import RunAgentInput

    from accounts.models import User
    from codebase.base import MergeRequest


# A claim that hasn't bumped last_active_at within this window is considered
# orphaned (worker crashed / OOM-killed before the streamer's finally ran) and
# can be taken over by a fresh claim. Live runs heartbeat well within this
# window via ``ChatThreadService.heartbeat``.
STALE_RUN_MINUTES = 30


def _extract_first_user_message(input_data: RunAgentInput) -> str:
    """Return the first non-empty content from a human/user role message."""
    for m in input_data.messages:
        role = (getattr(m, "role", None) or getattr(m, "type", "") or "").lower()
        if role not in ("user", "human"):
            continue
        content = getattr(m, "content", "")
        if isinstance(content, str) and content.strip():
            return content
    return ""


class ChatThreadService:
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
        """Atomic claim: succeeds if the slot is free OR its heartbeat is stale.

        Why: a worker crash (OOM, SIGKILL, ASGI transport error before the streaming
        body iterates) skips the streamer's ``finally`` so ``release_run`` never fires.
        Without the stale-takeover branch the thread would be unrecoverable forever.
        """
        stale_cutoff = timezone.now() - timedelta(minutes=STALE_RUN_MINUTES)
        free_or_stale = Q(active_run_id__isnull=True) | Q(last_active_at__lt=stale_cutoff)
        claimed = await ChatThread.objects.filter(Q(thread_id=thread_id) & free_or_stale).aupdate(
            active_run_id=run_id, last_active_at=timezone.now()
        )
        return bool(claimed)

    @staticmethod
    async def heartbeat(thread_id: str, run_id: str) -> None:
        """Bump ``last_active_at`` while the slot is still ours.

        Filtered on ``active_run_id=run_id`` so a delayed heartbeat from a previous
        run cannot keep a stale slot alive after another run took it over.
        """
        await ChatThread.objects.filter(thread_id=thread_id, active_run_id=run_id).aupdate(
            last_active_at=timezone.now()
        )

    @staticmethod
    async def release_run(thread_id: str, run_id: str) -> None:
        """Clear the slot only if we still hold it.

        The ``active_run_id=run_id`` guard prevents a delayed cleanup from stomping
        a freshly-claimed slot taken over via the stale path.
        """
        await ChatThread.objects.filter(thread_id=thread_id, active_run_id=run_id).aupdate(
            active_run_id=None, last_active_at=timezone.now()
        )

    @staticmethod
    async def persist_ref(thread_id: str, original_ref: str, mr: MergeRequest | dict | None) -> None:
        """Sync ``ChatThread.ref`` with the agent's final ``merge_request``.

        Accepts both a live ``MergeRequest`` instance and a dict (the snapshot
        gets rehydrated through the checkpointer as a plain dict, so resumed
        runs land here in dict shape).
        """
        if mr is None:
            return
        new_ref = mr.get("source_branch") if isinstance(mr, dict) else getattr(mr, "source_branch", None)
        if new_ref and new_ref != original_ref:
            await ChatThread.objects.filter(thread_id=thread_id).aupdate(ref=new_ref)
