from __future__ import annotations

from langchain.agents.middleware import TodoListMiddleware


class DAIVTodoListMiddleware(TodoListMiddleware):
    """DAIV's ``write_todos`` middleware — a marker subclass of upstream ``TodoListMiddleware``.

    DAIV ships its own todo guidance via a custom ``system_prompt`` and supplies its own
    ``TodoListMiddleware`` instance to both the main agent and its subagents (so they share
    the same guidance). The DAIV harness profile (:mod:`automation.agent.profile`) excludes
    the *upstream* ``TodoListMiddleware`` to drop the instance ``create_deep_agent``
    auto-adds — but ``_apply_excluded_middleware`` matches by **exact type**, so a plain
    ``TodoListMiddleware`` instance DAIV adds via ``user_middleware`` would be dropped along
    with the auto-added one, leaving the main agent with **no** ``write_todos`` tool at all.

    Subclassing sidesteps that: the profile excludes the exact base type, so the auto-added
    base instance is still removed while this subclass survives the filter. This is the same
    survive-the-harness-exclusion trick DAIV uses for
    :class:`automation.agent.middlewares.prompt_cache.AnthropicPromptCachingMiddleware`.
    """
