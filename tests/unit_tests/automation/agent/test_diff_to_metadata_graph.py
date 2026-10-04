from unittest.mock import MagicMock, patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from automation.agent.diff_to_metadata.graph import create_diff_to_metadata_graph
from automation.agent.diff_to_metadata.prompts import memory_section
from automation.agent.middlewares.memory import build_agents_memory_middleware


def test_memory_files_load_without_the_main_agents_guidelines(tmp_path):
    """This agent has no tools and its own rules for using memory, so the files come in a bare section."""
    ctx = MagicMock()
    ctx.gitrepo.working_dir = str(tmp_path / "repo")
    ctx.config.context_file_name = "AGENTS.md"

    with (
        patch("automation.agent.diff_to_metadata.graph.BaseAgent") as base_agent,
        patch(
            "automation.agent.diff_to_metadata.graph.build_agents_memory_middleware",
            wraps=build_agents_memory_middleware,
        ) as build_memory,
    ):
        base_agent.get_model.return_value = FakeListChatModel(responses=["ok"])
        create_diff_to_metadata_graph(model_names=["m"], ctx=ctx)

    assert build_memory.call_args.args[1:] == ("/repo", "AGENTS.md", memory_section)
