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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
async def test_consolidation_preserves_existing_memory_on_empty_output():
    # A whitespace-only response survives schema validation (min_length=1) but is empty
    # after strip() — it must NOT wipe the document or burn the pending observations.
    await _create_pending("group/project", 2)
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- keep me")

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning("   \n  ")),
    ):
        cfg.get_config.return_value = _enabled_config()
        await consolidate_memory_task.func("group/project")

    memory = await RepositoryMemory.objects.aget(repo_id="group/project")
    assert memory.content == "## Pitfalls\n- keep me"  # untouched
    assert memory.last_consolidated_at is None  # never marked consolidated
    assert (
        await MemoryObservation.objects.filter(repo_id="group/project", status=ObservationStatus.PENDING).acount() == 2
    )


@pytest.mark.django_db(transaction=True)
async def test_consolidation_feeds_existing_memory_to_llm():
    await _create_pending("group/project", 1)
    await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Existing\n- prior fact")
    llm = _structured_llm_returning("## Existing\n- prior fact\n- new fact")

    with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.tasks._build_structured_llm", return_value=llm):
        cfg.get_config.return_value = _enabled_config()
        await consolidate_memory_task.func("group/project")

    # The prior document is passed to the consolidation prompt so the LLM can merge it.
    (messages,), _ = llm.with_config.return_value.ainvoke.call_args
    human_content = messages[-1].content
    assert "## Existing\n- prior fact" in human_content


@pytest.mark.django_db(transaction=True)
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


def test_enforce_memory_budget_line_boundary():
    exactly_at_limit = "\n".join(f"line {i}" for i in range(MEMORY_MAX_LINES))
    # Exactly at the cap is left untouched; one over truncates to exactly the cap.
    assert enforce_memory_budget(exactly_at_limit) == exactly_at_limit
    one_over = "\n".join(f"line {i}" for i in range(MEMORY_MAX_LINES + 1))
    assert len(enforce_memory_budget(one_over).splitlines()) == MEMORY_MAX_LINES


def test_enforce_memory_budget_byte_boundary_keeps_valid_utf8():
    # A byte cap that lands inside a 2-byte char must drop the partial char (errors="ignore"),
    # never emit invalid UTF-8.
    content = "é" * 10  # 20 bytes
    result = enforce_memory_budget(content, max_bytes=5)  # cut mid-char (bytes 4-5 of the 3rd "é")
    assert result == "éé"  # partial trailing char dropped
    assert len(result.encode("utf-8")) <= 5
    result.encode("utf-8").decode("utf-8")  # round-trips, i.e. valid UTF-8
