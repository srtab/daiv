"""Static-source regression guards for middleware wiring and order in ``graph.py`` and ``subagents.py`` that a merge
conflict or refactor can break without failing a behaviour test, and the per-run settings the agent is built with."""

import inspect
import re
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module
from automation.agent.agent_settings import RunOverrides
from automation.agent.graph import create_daiv_agent
from automation.agent.middlewares.memory import RepositoryMemoryMiddleware
from automation.agent.middlewares.slash_commands import SlashCommandMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from codebase.repo_config import RepositoryConfig
from tests.unit_tests.conftest import FakeWorkspace, agent_settings, site_snapshot

# Matches a ``TodoListMiddleware(`` constructor call, rejecting any prefixed subclass. Catches both
# kwarg and positional forms, unlike a ``"TodoListMiddleware(system_prompt="`` substring check.
_BARE_TODO_CTOR = re.compile(r"(?<![A-Za-z0-9_])TodoListMiddleware\(")


def test_graph_gates_sandbox_middleware_on_the_workspace_shell():
    src = inspect.getsource(graph_module)
    assert "bash_tool_enabled = workspace.bash is not None" in src
    assert "*([SandboxMiddleware(agent_root=agent_root, workspace=workspace)] if bash_tool_enabled else [])" in src


def test_general_purpose_subagent_gates_sandbox_middleware_on_the_workspace_shell():
    src = inspect.getsource(subagents_module)
    assert "bash_tool_enabled = workspace.bash is not None" in src
    assert "if bash_tool_enabled:" in src
    assert "SandboxMiddleware(agent_root=REPO_PATH, workspace=workspace)" in src


def test_global_skills_source_is_workspace_skills():
    src = inspect.getsource(graph_module)
    assert "global_skills_source = SKILLS_PATH" in src, (
        "graph.py must use SKILLS_PATH (/workspace/skills) as the global-skills source"
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


_SITE = {
    "agent_model_name": "site-model",
    "agent_fallback_model_name": "site-fallback",
    "agent_thinking_level": "medium",
    "agent_fallback_thinking_level": "low",
    "agent_explore_model_name": "site-explore",
    "agent_explore_fallback_model_name": "",
    "agent_recursion_limit": 500,
    "web_fetch_enabled": False,
    "web_search_enabled": False,
}


async def _construct(
    *, site: dict | None = None, run: dict | None = None, slash_commands: bool = True, memory: bool = True
):
    """Build the agent through ``create_daiv_agent`` with its collaborators stubbed, and return what it was built with.

    ``site`` overrides site values, ``run`` is what the run asks for (``RunOverrides`` fields), and ``slash_commands``
    and ``memory`` are the repo's switches. The builder gets them resolved, as the executor hands them over.
    """
    settings = agent_settings(
        site=site_snapshot(**(_SITE | (site or {}))),
        repo=RepositoryConfig(slash_commands={"enabled": slash_commands}, memory={"enabled": memory}),
        run=RunOverrides(**(run or {})),
    )
    graph_patches = {
        "create_general_purpose_subagent": {},
        "load_custom_subagents": {"new": AsyncMock(return_value=[])},
        "create_deep_agent": {},
        "MCPToolkit": {"get_tools": AsyncMock(return_value=[])},
        "BaseAgent": {},
        "SkillsMiddleware": {},
        "GitMiddleware": {},
        "GitPlatformMiddleware": {},
        "AnthropicPromptCachingMiddleware": {},
        "ToolCallLoggingMiddleware": {},
    }
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(f"automation.agent.graph.{name}", **kwargs))
            for name, kwargs in graph_patches.items()
        }
        explore_models = stack.enter_context(patch("automation.agent.subagents.BaseAgent")).get_model
        stack.enter_context(patch("automation.agent.subagents.create_agent"))
        stack.enter_context(patch("automation.agent.middlewares.deferred_tools.deferred_settings", ENABLED=False))
        ctx = MagicMock()
        ctx.config.context_file_name = "AGENTS.md"
        await create_daiv_agent(settings=settings, ctx=ctx, workspace=FakeWorkspace(), auto_commit_changes=False)

    deep_agent = mocks["create_deep_agent"]
    return SimpleNamespace(
        models=[(c.kwargs["model"], c.kwargs["thinking_level"]) for c in mocks["BaseAgent"].get_model.call_args_list],
        explore_models=[c.kwargs["model"] for c in explore_models.call_args_list],
        middleware=deep_agent.call_args.kwargs["middleware"],
        subagent_web={
            toggle: mocks["create_general_purpose_subagent"].call_args.kwargs[toggle]
            for toggle in ("web_search_enabled", "web_fetch_enabled")
        },
        bound_config=deep_agent.return_value.with_config.call_args.args[0],
        settings=settings,
        git_middleware=mocks["GitMiddleware"],
    )


@pytest.mark.parametrize(
    ("site_fallback", "run", "expected"),
    [
        (
            "low",
            {"model_names": ("primary", "fb-1", "fb-2"), "agent_thinking_level": "high"},
            [("primary", "high"), ("fb-1", "low"), ("fb-2", "low")],
        ),
        (
            None,
            {"model_names": ("primary", "fb-1"), "agent_thinking_level": "high"},
            [("primary", "high"), ("fb-1", None)],
        ),
        (
            "minimal",
            {"model_names": ("primary", "fb-1"), "agent_thinking_level": None},
            [("primary", None), ("fb-1", "minimal")],
        ),
        ("low", {}, [("site-model", "medium"), ("site-fallback", "low")]),
    ],
    ids=["a-primary-override", "no-site-fallback-level", "primary-without-thinking", "the-default-chain"],
)
async def test_fallback_models_take_the_sites_fallback_thinking_level_whatever_the_primary_runs_with(
    site_fallback, run, expected
):
    built = await _construct(site={"agent_fallback_thinking_level": site_fallback}, run=run)

    assert built.models == expected


async def test_the_explore_subagent_runs_on_the_sites_explore_chain_whatever_the_run_chooses():
    site = {"agent_explore_model_name": "explore-a", "agent_explore_fallback_model_name": "explore-b"}
    built = await _construct(site=site, run={"model_names": ("run-a", "run-b"), "agent_thinking_level": "high"})

    assert built.explore_models == ["explore-a", "explore-b"]


async def test_the_explore_subagent_has_no_fallback_when_the_site_sets_none():
    built = await _construct(site={"agent_explore_model_name": "explore-a", "agent_explore_fallback_model_name": ""})

    assert built.explore_models == ["explore-a"]


async def test_the_agent_is_bound_to_the_sites_recursion_limit():
    built = await _construct(site={"agent_recursion_limit": 123})

    assert built.bound_config == {"recursion_limit": 123}


@pytest.mark.parametrize(
    ("site_on", "option", "expected"),
    [(True, None, True), (False, None, False), (True, False, False), (False, True, True)],
    ids=["site-on", "site-off", "option-turns-it-off", "option-turns-it-on"],
)
@pytest.mark.parametrize(
    ("toggle", "middleware_class"),
    [("web_search_enabled", WebSearchMiddleware), ("web_fetch_enabled", WebFetchMiddleware)],
)
async def test_a_web_toggle_follows_the_run_option_then_the_site(toggle, middleware_class, site_on, option, expected):
    built = await _construct(site={toggle: site_on}, run={toggle: option})

    assert any(isinstance(m, middleware_class) for m in built.middleware) is expected
    assert built.subagent_web[toggle] is expected


@pytest.mark.parametrize("enabled", [True, False])
async def test_slash_commands_follow_the_repos_switch(enabled):
    built = await _construct(slash_commands=enabled)

    assert any(isinstance(m, SlashCommandMiddleware) for m in built.middleware) is enabled


@pytest.mark.parametrize(
    ("site_on", "repo_on"), [(True, True), (False, True), (True, False)], ids=["both-on", "site-off", "repo-off"]
)
async def test_the_memory_middleware_gets_the_runs_memory_switch(site_on, repo_on):
    built = await _construct(site={"memory_enabled": site_on}, memory=repo_on)

    [memory] = [m for m in built.middleware if isinstance(m, RepositoryMemoryMiddleware)]
    assert memory.enabled is (site_on and repo_on)


async def test_the_git_middleware_gets_the_runs_settings():
    built = await _construct()

    assert built.git_middleware.call_args.kwargs["settings"] is built.settings
