from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memory.models import MemoryObservation, ObservationCategory, ObservationStatus, RepositoryMemory
from memory.schemas import ConsolidatedMemory
from memory.tasks import MEMORY_MAX_BYTES, MEMORY_MAX_LINES, consolidate_memory_task, enforce_memory_budget


def _enabled_config(enabled=True):
    config = MagicMock()
    config.memory.enabled = enabled
    config.models.agent.model = "openrouter:anthropic/claude-sonnet-4.6"
    config.models.agent.fallback_model = "openrouter:openai/gpt-5.3-codex"
    return config


def _structured_llm_returning(content: str):
    llm = MagicMock()
    llm.with_config.return_value.ainvoke = AsyncMock(return_value=ConsolidatedMemory(content=content))
    return llm


async def _create_pending(repo_id: str, n: int):
    return [
        await MemoryObservation.objects.acreate(
            repo_id=repo_id, category=ObservationCategory.PITFALL, content=f"observation number {i} with detail"
        )
        for i in range(n)
    ]


@pytest.mark.django_db
async def test_consolidation_writes_memory_and_marks_observations():
    await _create_pending("group/project", 3)
    doc = "## Pitfalls\n- observed thing"

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning(doc)),
    ):
        cfg.get_config.return_value = _enabled_config()
        await consolidate_memory_task.func("group/project")

    memory = await RepositoryMemory.objects.aget(repo_id="group/project")
    assert memory.content == doc
    assert memory.last_consolidated_at is not None
    assert (
        await MemoryObservation.objects.filter(repo_id="group/project", status=ObservationStatus.PENDING).acount() == 0
    )
    assert (
        await MemoryObservation.objects.filter(repo_id="group/project", status=ObservationStatus.CONSOLIDATED).acount()
        == 3
    )


@pytest.mark.django_db
async def test_consolidation_enforces_budget_on_oversized_output():
    await _create_pending("group/project", 1)
    oversized = "\n".join(f"- line {i}" for i in range(MEMORY_MAX_LINES * 2))

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning(oversized)),
    ):
        cfg.get_config.return_value = _enabled_config()
        await consolidate_memory_task.func("group/project")

    memory = await RepositoryMemory.objects.aget(repo_id="group/project")
    assert len(memory.content.splitlines()) <= MEMORY_MAX_LINES
    assert len(memory.content.encode("utf-8")) <= MEMORY_MAX_BYTES


@pytest.mark.django_db
async def test_consolidation_noop_when_disabled_or_nothing_pending():
    with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.tasks._build_structured_llm") as build:
        cfg.get_config.return_value = _enabled_config(enabled=False)
        await consolidate_memory_task.func("group/project")
        build.assert_not_called()

    with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.tasks._build_structured_llm") as build:
        cfg.get_config.return_value = _enabled_config()
        await consolidate_memory_task.func("group/empty-repo")
        build.assert_not_called()
    assert not await RepositoryMemory.objects.filter(repo_id="group/empty-repo").aexists()


@pytest.mark.django_db
async def test_consolidation_noop_when_model_unconfigured():
    await _create_pending("group/unconfigured-model", 1)

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks._build_structured_llm", side_effect=RuntimeError("no api key")),
    ):
        cfg.get_config.return_value = _enabled_config()
        await consolidate_memory_task.func("group/unconfigured-model")  # must not raise

    # No memory row created and observations remain pending when the model can't be built.
    assert not await RepositoryMemory.objects.filter(repo_id="group/unconfigured-model").aexists()
    assert (
        await MemoryObservation.objects.filter(
            repo_id="group/unconfigured-model", status=ObservationStatus.PENDING
        ).acount()
        == 1
    )


def test_enforce_memory_budget_truncates_lines_and_bytes():
    too_many_lines = "\n".join(str(i) for i in range(500))
    assert len(enforce_memory_budget(too_many_lines).splitlines()) == MEMORY_MAX_LINES

    too_many_bytes = "é" * MEMORY_MAX_BYTES  # 2 bytes each in UTF-8
    result = enforce_memory_budget(too_many_bytes)
    assert len(result.encode("utf-8")) <= MEMORY_MAX_BYTES

    fits = "## Build & test\n- short"
    assert enforce_memory_budget(fits) == fits
