"""Static-source regression guards.

These tests use ``inspect.getsource`` to assert that ``graph.py`` and
``subagents.py`` always pass ``backend`` and ``working_dir`` to
``SandboxMiddleware`` when sandbox is enabled — the merged middleware needs
both to mirror successful ``write_file``/``edit_file`` calls to the sandbox.
They are cheap and catch the kind of merge-conflict or refactor that silently
disables sandbox sync.
"""

import inspect

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module


def test_graph_passes_backend_and_working_dir_to_sandbox_middleware():
    src = inspect.getsource(graph_module)
    assert "SandboxMiddleware(backend=backend, working_dir=agent_path)" in src, (
        "graph.py must construct SandboxMiddleware with backend and working_dir"
    )
    assert "_sandbox_enabled" in src, "graph.py must gate SandboxMiddleware on _sandbox_enabled"


def test_general_purpose_subagent_passes_backend_and_working_dir_to_sandbox_middleware():
    src = inspect.getsource(subagents_module)
    assert (
        "SandboxMiddleware(" in src
        and "backend=backend" in src
        and "working_dir=Path(runtime.gitrepo.working_dir)" in src
    ), "subagents.py must construct SandboxMiddleware with backend and working_dir"
    assert "if sandbox_enabled:" in src, "subagents.py must gate SandboxMiddleware on sandbox_enabled"
