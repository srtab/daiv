from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("daiv.agent")

MEMORY_SECTION_HEADER = (
    "# Learned repository memory (auto-generated from past runs; may be stale — verify before relying on it)"
)


class _Unloaded:
    """Sentinel: memory not fetched yet (distinguishes from '' = fetched, empty)."""


class RepositoryMemoryMiddleware(AgentMiddleware):
    """Append the consolidated repository memory document to the system prompt.

    Injection happens via the prompt — NOT a workdir file — because GitMiddleware
    auto-commits filesystem changes and a materialized memory file would pollute
    commits. The ``RepositoryMemory`` row is loaded once per middleware instance
    (one instance per agent run) and the middleware silently no-ops when the
    feature is disabled (per-repo or site-wide), no memory exists, or the lookup
    fails: memory must never block or fail a run.

    Registered after ``dynamic_daiv_system_prompt`` in ``create_daiv_agent`` so it
    sees (and appends to) the fully composed system prompt.
    """

    def __init__(self) -> None:
        super().__init__()
        self._content: str | type[_Unloaded] = _Unloaded

    async def _load_content(self, repo_id: str) -> str:
        from memory.models import RepositoryMemory

        if self._content is _Unloaded:
            try:
                memory = await RepositoryMemory.objects.filter(repo_id=repo_id).afirst()
                self._content = memory.content.strip() if memory else ""
            except Exception:
                logger.exception("RepositoryMemoryMiddleware: failed to load memory for repo %s", repo_id)
                self._content = ""
        return cast("str", self._content)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        context = request.runtime.context
        # Cheapest-first: the in-memory per-repo flag short-circuits before the site-wide
        # setting read. Either being off is a silent no-op (memory must never block a run).
        if not context.config.memory.enabled or not site_settings.memory_enabled:
            return await handler(request)

        content = await self._load_content(context.repository.slug)
        if not content:
            return await handler(request)

        request = request.override(system_prompt=f"{request.system_prompt}\n\n{MEMORY_SECTION_HEADER}\n{content}")
        return await handler(request)
