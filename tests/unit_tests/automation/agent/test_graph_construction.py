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
