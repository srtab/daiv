"""Static-source regression guards for middleware wiring and order in ``graph.py`` and ``subagents.py`` that a merge
conflict or refactor can break without failing a behaviour test."""

import inspect
import re

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module

# Matches a ``TodoListMiddleware(`` constructor call, rejecting any prefixed subclass. Catches both
# kwarg and positional forms, unlike a ``"TodoListMiddleware(system_prompt="`` substring check.
_BARE_TODO_CTOR = re.compile(r"(?<![A-Za-z0-9_])TodoListMiddleware\(")


def test_graph_gates_sandbox_middleware_on_the_workspace_shell():
    src = inspect.getsource(graph_module)
    assert "bash_tool_enabled = workspace.bash is not None" in src
    assert "*([SandboxMiddleware(agent_root=agent_root, workspace=workspace)] if bash_tool_enabled else [])" in src


def test_general_purpose_subagent_gates_sandbox_middleware_on_the_workspace_shell():
    src = inspect.getsource(subagents_module)
    assert "if workspace.bash is not None:" in src
    assert "SandboxMiddleware(agent_root=REPO_PATH, workspace=workspace)" in src


def test_graph_uses_fallback_thinking_level_standalone():
    src = inspect.getsource(graph_module)
    assert "fallback_thinking_level = site_settings.agent_fallback_thinking_level" in src, (
        "graph.py must bind fallback_thinking_level directly from site_settings — no coalesce "
        "to the runtime thinking_level, which may carry a per-turn primary override"
    )
    assert "site_settings.agent_fallback_thinking_level or" not in src, (
        "graph.py must NOT coalesce agent_fallback_thinking_level to another value"
    )


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
    sandbox = src.index("SandboxMiddleware(agent_root=agent_root, workspace=workspace)")
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


def test_skills_middleware_copies_global_skills_unless_the_workspace_provisions_them():
    src = inspect.getsource(graph_module)
    skills_call = _balanced_call_args(src, "SkillsMiddleware(")
    assert "copy_global_skills=not workspace.provisions_skills" in skills_call


def test_slash_command_middleware_receives_subagents():
    src = inspect.getsource(graph_module)
    assert "SlashCommandMiddleware(subagents=subagents)" in src


def test_git_middleware_registered_after_sandbox_middleware():
    # before_agent hooks run in registration order, so GitMiddleware must come after SandboxMiddleware — otherwise
    # its pre-run check would run git in a session nothing has acquired yet.
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


def _assert_builds_todo_middleware(module):
    # DAIV supplies its own todo middleware with custom guidance. deepagents 0.7 stopped
    # auto-adding TodoListMiddleware, so this instance is the only source of write_todos —
    # dropping the call leaves the agent with no todo tool at all.
    src = inspect.getsource(module)
    assert _BARE_TODO_CTOR.search(src), f"{module.__name__} must build a TodoListMiddleware instance"


def test_graph_builds_todo_middleware():
    _assert_builds_todo_middleware(graph_module)


def test_subagents_build_todo_middleware():
    _assert_builds_todo_middleware(subagents_module)


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


def test_graph_uses_daiv_filesystem_middleware():
    """The main agent greps too and shares the paths-only default with the detectors, so it must
    get DAIV's filesystem subclass (which carries the output-mode label), not plain upstream.
    """
    src = inspect.getsource(graph_module)

    assert "DAIVFilesystemMiddleware(" in src
    assert not re.search(r"(?<![A-Za-z0-9_])FilesystemMiddleware\(", src), "graph must not wire upstream"


def test_context_usage_middleware_registered_after_model_fallback():
    # ModelFallbackMiddleware retries the INNER chain with the fallback model, so only a
    # middleware listed after it meters the model that actually served the call.
    src = inspect.getsource(graph_module)
    fallback = src.index("ModelFallbackMiddleware(fallback_models[0]")
    meter = src.index("ContextUsageMiddleware()")
    assert fallback < meter, "ContextUsageMiddleware must be registered after ModelFallbackMiddleware"
