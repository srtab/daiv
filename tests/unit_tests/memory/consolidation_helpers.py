from unittest.mock import AsyncMock, MagicMock

from memory.constants import MEMORY_MAX_BYTES, MEMORY_MAX_LINES
from memory.models import MemoryEntry, MemoryObservation, ObservationCategory
from memory.schemas import MemoryOperation, MemoryOperations

from codebase.repo_config import RepositoryConfig
from tests.unit_tests.conftest import site_snapshot


def _enabled_config(enabled=True):
    return RepositoryConfig(
        memory={"enabled": enabled},
        models={
            "agent": {
                "model": "openrouter:anthropic/claude-sonnet-4.6",
                "fallback_model": "openrouter:openai/gpt-5.3-codex",
            }
        },
    )


def _site_settings(**overrides):
    """Mock of the site-settings singleton: the memory defaults consolidation reads, and the one snapshot of them."""
    fields = {
        "memory_enabled": True,
        "memory_consolidation_model_name": None,  # empty → reuse repo agent model
        "memory_max_lines": MEMORY_MAX_LINES,
        "memory_max_bytes": MEMORY_MAX_BYTES,
    } | overrides
    ss = MagicMock()
    ss.snapshot.return_value = site_snapshot(**fields)
    for key, value in fields.items():
        setattr(ss, key, value)
    return ss


def _structured_llm_returning(*operations: MemoryOperation):
    llm = MagicMock()
    llm.with_config.return_value.ainvoke = AsyncMock(return_value=MemoryOperations(operations=list(operations)))
    return llm


async def _observation(repo_id="group/project", category=ObservationCategory.PITFALL, content="something learned here"):
    return await MemoryObservation.objects.acreate(repo_id=repo_id, category=category, content=content)


async def _entry(content, category=ObservationCategory.PITFALL, repo_id="group/project"):
    return await MemoryEntry.objects.acreate(repo_id=repo_id, category=category, content=content)
