from unittest.mock import MagicMock, patch

from deepagents.middleware.memory import MemoryMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from automation.agent.diff_to_metadata.graph import create_diff_to_metadata_graph
from automation.agent.prompts import AGENTS_MEMORY_SYSTEM_PROMPT


def test_memory_files_load_with_daivs_guidelines(tmp_path):
    ctx = MagicMock()
    ctx.gitrepo.working_dir = str(tmp_path / "repo")
    ctx.config.context_file_name = "AGENTS.md"

    with (
        patch("automation.agent.diff_to_metadata.graph.BaseAgent") as base_agent,
        patch("automation.agent.diff_to_metadata.graph.MemoryMiddleware", wraps=MemoryMiddleware) as memory,
    ):
        base_agent.get_model.return_value = FakeListChatModel(responses=["ok"])
        create_diff_to_metadata_graph(model_names=["m"], ctx=ctx)

    assert memory.call_args.kwargs["system_prompt"] == AGENTS_MEMORY_SYSTEM_PROMPT
