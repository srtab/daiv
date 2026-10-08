from unittest.mock import MagicMock, Mock, patch

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.memory import MemoryMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda

from automation.agent.chat_models import ChatOpenRouter
from automation.agent.constants import REPO_PATH
from automation.agent.diff_to_metadata.graph import create_diff_to_metadata_graph
from automation.agent.diff_to_metadata.prompts import memory_section
from automation.agent.middlewares.file_system import build_disk_workspace_backend
from automation.agent.middlewares.memory import build_agents_memory_middleware
from tests.unit_tests.conftest import OPENROUTER_CACHE_SETTINGS, model_attempts_with_failing_primary


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


def test_memory_files_load_without_the_main_agents_guidelines():
    """This agent has no tools and its own rules for using memory, so the files come in a bare section."""
    ctx = MagicMock()
    ctx.config.context_file_name = "AGENTS.md"

    with (
        patch("automation.agent.diff_to_metadata.graph.BaseAgent") as base_agent,
        patch(
            "automation.agent.diff_to_metadata.graph.build_agents_memory_middleware",
            wraps=build_agents_memory_middleware,
        ) as build_memory,
    ):
        base_agent.get_model.return_value = FakeListChatModel(responses=["ok"])
        create_diff_to_metadata_graph(model_names=["m"], ctx=ctx, backend=MagicMock())

    assert build_memory.call_args.args[1:] == (REPO_PATH, "AGENTS.md", memory_section)


async def test_fallback_attempt_carries_no_openrouter_cache_control():
    primary = ChatOpenRouter(model="anthropic/claude-haiku-4.5", api_key="x")
    fallback = ChatOpenRouter(model="openai/gpt-5.4", api_key="x")
    built = {"primary": primary, "fallback": fallback}

    with (
        patch("automation.agent.diff_to_metadata.graph.BaseAgent.get_model", side_effect=lambda *, model: built[model]),
        patch("automation.agent.diff_to_metadata.graph.create_agent") as create_agent,
    ):
        create_diff_to_metadata_graph(
            ["primary", "fallback"], ctx=Mock(), backend=Mock(spec=BackendProtocol), include_commit_message=False
        )

    assert await model_attempts_with_failing_primary(create_agent.call_args.kwargs["middleware"], primary) == [
        (primary, OPENROUTER_CACHE_SETTINGS),
        (fallback, {}),
    ]
