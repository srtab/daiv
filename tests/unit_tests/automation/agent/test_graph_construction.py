"""Static-source regression guards.

These tests use ``inspect.getsource`` to assert that ``graph.py`` and
``subagents.py`` always wire up the sandbox-sync middleware when sandbox is
enabled. They are cheap and catch the kind of merge-conflict or refactor
that silently disables sandbox sync.
"""

import inspect

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module


def test_graph_wires_sandbox_sync_middleware():
    src = inspect.getsource(graph_module)
    assert "FilesystemSandboxSyncMiddleware(backend=backend, working_dir=" in src, (
        "graph.py must construct FilesystemSandboxSyncMiddleware with backend and working_dir"
    )
    assert "_sandbox_enabled" in src, "graph.py must gate the sandbox-sync middleware on _sandbox_enabled"


def test_general_purpose_subagent_wires_sandbox_sync_middleware():
    src = inspect.getsource(subagents_module)
    assert "FilesystemSandboxSyncMiddleware(backend=backend, working_dir=" in src, (
        "subagents.py must construct FilesystemSandboxSyncMiddleware with backend and working_dir"
    )
    assert "if sandbox_enabled:" in src, "subagents.py must gate the sandbox-sync middleware on sandbox_enabled"
