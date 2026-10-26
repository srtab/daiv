from pathlib import Path

from plugins.runtime import ResolvedPlugin


def test_workspace_root():
    plugin = ResolvedPlugin(name="devkit", host_root=Path("/x"), skills=(), agents=())

    assert plugin.workspace_root == "/workspace/plugins/devkit"
