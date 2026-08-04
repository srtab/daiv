from unittest.mock import AsyncMock, MagicMock

from memory.constants import MEMORY_MAX_BYTES, MEMORY_MAX_LINES
from memory.models import MemoryEntry, MemoryObservation, ObservationCategory
from memory.schemas import MemoryOperation, MemoryOperations


def _enabled_config(enabled=True):
    config = MagicMock()
    config.memory.enabled = enabled
    config.models.agent.model = "openrouter:anthropic/claude-sonnet-4.6"
    config.models.agent.fallback_model = "openrouter:openai/gpt-5.3-codex"
    return config


def _site_settings(**overrides):
    """Mock of the site-settings singleton with the memory defaults consolidation reads."""
    ss = MagicMock()
    ss.memory_enabled = True
    ss.memory_consolidation_model_name = None  # empty → reuse repo agent model
    ss.memory_max_lines = MEMORY_MAX_LINES
    ss.memory_max_bytes = MEMORY_MAX_BYTES
    for key, value in overrides.items():
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
