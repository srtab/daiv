"""Static-source regression guards.

These tests use ``inspect.getsource`` to assert that ``graph.py`` and
``subagents.py`` always pass the eager-sync flag through to
``FilesystemMiddleware``. They are cheap and catch the kind of merge-conflict
or refactor that silently disables sandbox sync.
"""

import inspect

from automation.agent import graph as graph_module
from automation.agent import subagents as subagents_module


def test_graph_passes_sandbox_sync_to_filesystem_middleware():
    src = inspect.getsource(graph_module)
    assert "sandbox_sync=_sandbox_enabled" in src, (
        "graph.py must pass sandbox_sync=_sandbox_enabled to FilesystemMiddleware"
    )
    assert "working_dir=" in src, "graph.py must pass working_dir to FilesystemMiddleware"


def test_general_purpose_subagent_passes_sandbox_sync():
    src = inspect.getsource(subagents_module)
    assert "sandbox_sync=sandbox_enabled" in src, (
        "subagents.py must pass sandbox_sync=sandbox_enabled to FilesystemMiddleware"
    )
