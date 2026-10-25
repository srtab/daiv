from pathlib import Path

from plugins.runtime import ResolvedPlugin


def test_workspace_root():
    plugin = ResolvedPlugin(name="devkit", commit="a" * 40, host_root=Path("/x"), skills=(), agents=())

    assert plugin.workspace_root == "/workspace/plugins/devkit"
