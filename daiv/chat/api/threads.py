from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sessions.models import Session, SessionOrigin

from automation.titling.services import TitlerService
from automation.titling.tasks import generate_title_task

if TYPE_CHECKING:
    from ag_ui.core import RunAgentInput
    from sandbox_envs.models import SandboxEnvironment

    from accounts.models import User
    from codebase.base import MergeRequest

logger = logging.getLogger("daiv.chat")


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


class ChatSessionService:
    @staticmethod
    async def get_or_create_for_user(
        *,
        user: User,
        thread_id: str,
        repo_id: str,
        ref: str,
        input_data: RunAgentInput,
        sandbox_environment: SandboxEnvironment | None = None,
        agent_model: str = "",
        agent_thinking_level: str = "",
    ) -> tuple[Session, bool]:
        """First sight of ``thread_id`` creates a chat-origin ``Session`` under ``user``;
        later calls return the existing row regardless of owner. Caller must enforce
        ownership.

        ``agent_model`` and ``agent_thinking_level`` are pinned at session creation:
        they're written to ``defaults`` so the first turn fixes the override and
        subsequent turns ignore client-supplied values (same lock semantics as
        ``sandbox_environment``). The boolean return flag lets callers detect the
        existing-session case and reject a client that tries to change the override
        after the first turn — see ``chat.api.views.create_chat_completion``.
        """
        first_message = _extract_first_user_message(input_data)
        defaults = {
            "origin": SessionOrigin.CHAT,
            "user": user,
            "repo_id": repo_id,
            "ref": ref,
            "title": TitlerService.heuristic(first_message),
            "agent_model": agent_model,
            "agent_thinking_level": agent_thinking_level,
        }
        if sandbox_environment is not None:
            defaults["sandbox_environment"] = sandbox_environment
        session, created = await Session.objects.aget_or_create(thread_id=thread_id, defaults=defaults)
        if created and first_message:
            try:
                await generate_title_task.aenqueue(
                    entity_type="session", pk=session.thread_id, prompt=first_message, repo_id=repo_id, ref=ref
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to enqueue title task for session %s", session.thread_id)
        return session, created

    @staticmethod
    async def persist_ref(thread_id: str, original_ref: str, mr: MergeRequest | dict | None) -> None:
        """Sync ``Session.ref`` with the agent's final ``merge_request``.

        Accepts both a live ``MergeRequest`` instance and a dict (the snapshot
        gets rehydrated through the checkpointer as a plain dict, so resumed
        runs land here in dict shape).
        """
        if mr is None:
            return
        new_ref = mr.get("source_branch") if isinstance(mr, dict) else getattr(mr, "source_branch", None)
        if new_ref and new_ref != original_ref:
            await Session.objects.filter(thread_id=thread_id).aupdate(ref=new_ref)
