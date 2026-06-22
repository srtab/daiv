"""Static-source regression guards.

These tests use ``inspect.getsource`` to assert that ``graph.py`` and ``subagents.py``
always pass ``backend`` and ``agent_root`` to ``SandboxMiddleware`` when sandbox is
enabled — the merged middleware needs both to validate inbound paths and mirror
successful ``write_file``/``edit_file`` calls to the sandbox. They catch the kind
of merge-conflict or refactor that silently disables sandbox sync.
"""

import inspect
import re

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module

# Matches a constructor call to the *bare* upstream ``TodoListMiddleware(`` while excluding the
# ``DAIVTodoListMiddleware(`` subclass (the negative lookbehind rejects the ``…V`` prefix). Catches
# both kwarg and positional reverts, unlike a ``"TodoListMiddleware(system_prompt="`` substring check.
_BARE_TODO_CTOR = re.compile(r"(?<![A-Za-z0-9_])TodoListMiddleware\(")


def test_graph_passes_backend_and_agent_root_to_sandbox_middleware():
    src = inspect.getsource(graph_module)
    assert "SandboxMiddleware(" in src and "backend=backend" in src, (
        "graph.py must construct SandboxMiddleware with backend"
    )
    assert "agent_root=agent_root" in src, "graph.py must pass agent_root to SandboxMiddleware"
    assert "_sandbox_enabled" in src, "graph.py must gate SandboxMiddleware on _sandbox_enabled"


def test_general_purpose_subagent_passes_backend_and_agent_root_to_sandbox_middleware():
    src = inspect.getsource(subagents_module)
    assert "SandboxMiddleware(" in src and "backend=backend" in src, (
        "subagents.py must construct SandboxMiddleware with backend"
    )
    assert "agent_root=REPO_PATH" in src, "subagents.py must pass agent_root to SandboxMiddleware"
    assert "if sandbox_enabled:" in src, "subagents.py must gate SandboxMiddleware on sandbox_enabled"


def test_graph_uses_fallback_thinking_level_standalone():
    src = inspect.getsource(graph_module)
    assert "fallback_thinking_level = site_settings.agent_fallback_thinking_level" in src, (
        "graph.py must bind fallback_thinking_level directly from site_settings — no coalesce "
        "to the runtime thinking_level, which may carry a per-turn primary override"
    )
    assert "site_settings.agent_fallback_thinking_level or" not in src, (
        "graph.py must NOT coalesce agent_fallback_thinking_level to another value"
    )


def test_graph_constructs_sandbox_backend_without_root():
    src = inspect.getsource(graph_module)
    assert "SandboxFileBackend(client=run_client)" in src, (
        "graph.py must construct SandboxFileBackend(client=run_client) with no root — the agent uses "
        "sandbox-absolute paths and the run client is injected by construction"
    )
    assert "SandboxFileBackend(root=" not in src, "graph.py must NOT pass a root to SandboxFileBackend (pass-through)"


def test_global_skills_source_is_workspace_skills():
    src = inspect.getsource(graph_module)
    # global_skills_source is now unconditional (/workspace/skills) across sandbox and disk modes.
    assert "global_skills_source = SKILLS_PATH" in src, (
        "graph.py sandbox branch must use SKILLS_PATH (/workspace/skills) as the global-skills source"
    )


def test_middleware_order_slash_then_sandbox_then_skills():
    src = inspect.getsource(graph_module)
    # SlashCommandMiddleware must run before SandboxMiddleware (so /clear etc. don't start a sandbox),
    # and SkillsMiddleware must run AFTER SandboxMiddleware (so the backend is bound + seeded before
    # discovery reads it).
    slash = src.index("SlashCommandMiddleware(")
    sandbox = src.index("SandboxMiddleware(agent_root=agent_root, client=run_client, sandbox_backend=sandbox_backend)")
    skills = src.index("SkillsMiddleware(")
    assert slash < sandbox < skills, "order must be SlashCommandMiddleware -> SandboxMiddleware -> SkillsMiddleware"


def _balanced_call_args(src: str, callee: str) -> str:
    """Return the argument text inside ``callee(...)`` via balanced-paren matching."""
    start = src.index(callee) + len(callee)
    depth, i = 1, start
    while i < len(src) and depth > 0:
        depth += {"(": 1, ")": -1}.get(src[i], 0)
        i += 1
    return src[start : i - 1]


def test_skills_middleware_receives_sandbox_enabled_flag():
    src = inspect.getsource(graph_module)
    # Assert the flag is passed INSIDE the SkillsMiddleware(...) call, not just somewhere in the
    # file (it also appears on the subagent factory calls), so this guards the real wiring.
    skills_call = _balanced_call_args(src, "SkillsMiddleware(")
    assert "sandbox_enabled=_sandbox_enabled" in skills_call, "SkillsMiddleware must receive the sandbox_enabled flag"


def test_slash_command_middleware_receives_subagents():
    src = inspect.getsource(graph_module)
    assert "SlashCommandMiddleware(subagents=subagents)" in src


def test_git_middleware_registered_after_sandbox_middleware():
    # after_agent hooks run in REVERSE registration order, so GitMiddleware must come after
    # SandboxMiddleware — otherwise turn-end publish/patch-capture would hit a closed session.
    src = inspect.getsource(graph_module)
    sandbox = src.index("SandboxMiddleware(agent_root=agent_root")
    git = src.index("GitMiddleware(")
    assert sandbox < git, "GitMiddleware must be registered after SandboxMiddleware"


def test_git_middleware_receives_capture_patch_flag():
    src = inspect.getsource(graph_module)
    git_call = _balanced_call_args(src, "GitMiddleware(")
    assert "capture_patch=capture_patch" in git_call, (
        "GitMiddleware must receive the capture_patch flag from create_daiv_agent — eval harnesses "
        "rely on it to read the run's patch from ainvoke output state"
    )


def _assert_uses_daiv_todo_subclass(module):
    # DAIV's todo middleware must be the DAIVTodoListMiddleware subclass, never the bare upstream
    # class: the harness profile excludes the base by exact type, so a bare instance would be dropped
    # (alongside the one create_deep_agent auto-adds), leaving the agent with no write_todos.
    src = inspect.getsource(module)
    assert "DAIVTodoListMiddleware(" in src, f"{module.__name__} must build todo middleware as DAIVTodoListMiddleware"
    assert not _BARE_TODO_CTOR.search(src), f"{module.__name__} must NOT construct the bare upstream TodoListMiddleware"


def test_graph_uses_daiv_todo_subclass_not_upstream_base():
    # The main agent runs through create_deep_agent, where a bare TodoListMiddleware would be excluded.
    _assert_uses_daiv_todo_subclass(graph_module)


def test_subagents_use_daiv_todo_subclass_not_upstream_base():
    # Subagents don't hit the profile filter, but use the same subclass uniformly (mirrors how DAIV's
    # AnthropicPromptCachingMiddleware subclass is used in both the main agent and subagents).
    _assert_uses_daiv_todo_subclass(subagents_module)


def test_slash_command_middleware_registered_only_when_enabled():
    # The enabled check lives at registration time (like sandbox/web middleware), not inside the
    # middleware — so a disabled config drops the middleware entirely rather than no-op'ing per turn.
    src = inspect.getsource(graph_module)
    assert "*([SlashCommandMiddleware(subagents=subagents)] if ctx.config.slash_commands.enabled else [])" in src, (
        "SlashCommandMiddleware must be conditionally registered on ctx.config.slash_commands.enabled"
    )


def test_parent_stack_includes_loop_breaker_with_finalize_terminal():
    src = inspect.getsource(graph_module)
    breaker_call = _balanced_call_args(src, "LoopBreakerMiddleware(")
    assert 'terminal="finalize"' in breaker_call, (
        "graph.py must register LoopBreakerMiddleware with terminal='finalize' so a parent loop ends "
        "cleanly (after_agent hooks run) instead of raising and discarding work"
    )


def test_loop_breaker_registered_before_prompt_caching():
    # The injected reminder must be visible to AnthropicPromptCachingMiddleware, so the breaker is
    # registered before it (same rationale as StepBudgetMiddleware).
    src = inspect.getsource(graph_module)
    breaker = src.index("LoopBreakerMiddleware(")
    caching = src.index("AnthropicPromptCachingMiddleware(")
    assert breaker < caching, "LoopBreakerMiddleware must be registered before AnthropicPromptCachingMiddleware"


def test_repository_memory_middleware_registered_after_dynamic_prompt():
    # RepositoryMemoryMiddleware appends to the system prompt, so it must be registered AFTER
    # dynamic_daiv_system_prompt — otherwise it would append to a half-built prompt. rindex targets
    # the registration entry (last occurrence), not the function definition/import.
    src = inspect.getsource(graph_module)
    prompt = src.rindex("dynamic_daiv_system_prompt")
    memory_mw = src.index("RepositoryMemoryMiddleware(")
    assert prompt < memory_mw, "RepositoryMemoryMiddleware must be registered after dynamic_daiv_system_prompt"
