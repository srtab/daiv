from unittest.mock import Mock, patch

from deepagents.middleware.memory import MemoryMiddleware
from langchain_core.runnables import RunnableLambda

from automation.agent.diff_to_metadata.graph import create_diff_to_metadata_graph
from automation.agent.middlewares.file_system import build_disk_workspace_backend


async def test_context_files_are_read_through_the_workspace_backend(tmp_path):
    """A sandbox run edits the context file in the sandbox, not in the worker's clone, so the metadata reads the run's
    workspace."""
    clone = tmp_path / "clone"
    (clone / ".agents").mkdir(parents=True)
    (clone / "AGENTS.md").write_text("Use conventional commits.")
    (clone / ".agents" / "AGENTS.md").write_text("Scope is the app name.")
    ctx = Mock()
    ctx.config.context_file_name = "AGENTS.md"
    middlewares = []

    def fake_create_agent(**kwargs):
        middlewares.extend(kwargs["middleware"])
        return RunnableLambda(lambda _input: {})

    with (
        patch("automation.agent.diff_to_metadata.graph.BaseAgent"),
        patch("automation.agent.diff_to_metadata.graph.create_agent", side_effect=fake_create_agent),
    ):
        create_diff_to_metadata_graph(
            ["model"], ctx=ctx, backend=build_disk_workspace_backend(clone), include_pr_metadata=False
        )
    memory = next(middleware for middleware in middlewares if isinstance(middleware, MemoryMiddleware))

    update = await memory.abefore_agent({}, Mock(), {})

    assert update["memory_contents"] == {
        "/workspace/repo/AGENTS.md": "Use conventional commits.",
        "/workspace/repo/.agents/AGENTS.md": "Scope is the app name.",
    }
