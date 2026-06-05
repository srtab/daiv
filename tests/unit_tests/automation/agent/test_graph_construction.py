"""Static-source regression guards.

These tests use ``inspect.getsource`` to assert that ``graph.py`` and ``subagents.py``
always pass ``backend`` and ``agent_root`` to ``SandboxMiddleware`` when sandbox is
enabled — the merged middleware needs both to validate inbound paths and mirror
successful ``write_file``/``edit_file`` calls to the sandbox. They catch the kind
of merge-conflict or refactor that silently disables sandbox sync.
"""

import inspect

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module


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
    assert 'agent_root=f"/{agent_path.name}"' in src, "subagents.py must pass agent_root to SandboxMiddleware"
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


def test_slash_command_middleware_registered_only_when_enabled():
    # The enabled check lives at registration time (like sandbox/web middleware), not inside the
    # middleware — so a disabled config drops the middleware entirely rather than no-op'ing per turn.
    src = inspect.getsource(graph_module)
    assert "*([SlashCommandMiddleware(subagents=subagents)] if ctx.config.slash_commands.enabled else [])" in src, (
        "SlashCommandMiddleware must be conditionally registered on ctx.config.slash_commands.enabled"
    )
