"""Regression guards for the DAIV harness profile's middleware exclusions.

The profile excludes the *upstream* ``TodoListMiddleware`` (and the upstream
``AnthropicPromptCachingMiddleware``) so ``create_deep_agent``'s auto-added
instance is dropped. Because the exclusion matches by **exact type**, DAIV must
supply its own middleware as a *subclass* — otherwise its instance is dropped
alongside the auto-added one and the main agent loses the ``write_todos`` tool
entirely. These tests pin that contract.
"""

from deepagents._excluded_middleware import _apply_excluded_middleware
from langchain.agents.middleware import TodoListMiddleware

from automation.agent.middlewares.todos import DAIVTodoListMiddleware
from automation.agent.profile import DAIV_HARNESS_PROFILE


def test_daiv_todo_subclass_is_distinct_from_upstream():
    # The fix relies on DAIVTodoListMiddleware being a *subclass* (distinct exact type)
    # of the upstream class the profile excludes.
    assert issubclass(DAIVTodoListMiddleware, TodoListMiddleware)
    assert DAIVTodoListMiddleware is not TodoListMiddleware


def test_daiv_todo_middleware_registers_write_todos_tool():
    # Subclassing must not break tool registration — the whole point is to keep write_todos.
    mw = DAIVTodoListMiddleware(system_prompt="todo guidance")
    assert any(tool.name == "write_todos" for tool in mw.tools)


def test_profile_excludes_upstream_todo_but_preserves_daiv_subclass():
    # Reproduce the main-agent stack create_deep_agent assembles: the auto-added upstream
    # instance plus DAIV's own subclass, both present before the profile filter runs.
    auto_added = TodoListMiddleware()
    daiv_own = DAIVTodoListMiddleware(system_prompt="todo guidance")

    filtered = _apply_excluded_middleware([auto_added, daiv_own], DAIV_HARNESS_PROFILE)
    survivor_types = [type(m) for m in filtered]

    # The auto-added upstream instance is dropped (exact-type match) ...
    assert TodoListMiddleware not in survivor_types
    # ... while DAIV's subclass survives — exactly one middleware remains, nothing else.
    assert DAIVTodoListMiddleware in survivor_types
    assert len(filtered) == 1
    # End-to-end: the surviving middleware still exposes write_todos to the agent.
    assert any(tool.name == "write_todos" for m in filtered for tool in m.tools)


def test_profile_lists_upstream_todo_middleware_as_excluded():
    # Guard the exclusion entry itself: it must target the upstream base class (so the
    # auto-added instance is what gets dropped), never DAIV's subclass.
    assert TodoListMiddleware in DAIV_HARNESS_PROFILE.excluded_middleware
    assert DAIVTodoListMiddleware not in DAIV_HARNESS_PROFILE.excluded_middleware
