from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from langchain_core.messages import AIMessage, HumanMessage
from memory.models import MemoryObservation, ObservationStatus, RepositoryMemory
from memory.schemas import ExtractedObservation, ExtractedObservations
from memory.tasks import CONSOLIDATION_MIN_PENDING, extract_observations_task


def _enabled_config(enabled=True):
    config = MagicMock()
    config.memory.enabled = enabled
    return config


def _checkpointer_with(messages):
    tup = None
    if messages is not None:
        tup = MagicMock()
        tup.checkpoint = {"channel_values": {"messages": messages}}

    cp = MagicMock()
    cp.aget_tuple = AsyncMock(return_value=tup)

    @asynccontextmanager
    async def _open():
        yield cp

    return _open


def _structured_llm_returning(observations):
    llm = MagicMock()
    llm.with_config.return_value.ainvoke = AsyncMock(return_value=ExtractedObservations(observations=observations))
    return llm


async def _create_activity(**kwargs):
    defaults = {
        "trigger_type": TriggerType.API_JOB,
        "repo_id": "group/project",
        "status": ActivityStatus.SUCCESSFUL,
        "thread_id": "thread-1",
    }
    defaults.update(kwargs)
    return await Activity.objects.acreate(**defaults)


TRANSCRIPT = [HumanMessage(content="fix the bug"), AIMessage(content="done, ran make test")]


@pytest.mark.django_db(transaction=True)
async def test_extraction_creates_observation_rows():
    activity = await _create_activity()
    extracted = [
        ExtractedObservation(category="build_test", content="`make test` needs LANGCHAIN_TRACING_V2=false set"),
        ExtractedObservation(category="pitfall", content="editing pyproject.toml directly breaks uv lock sync"),
    ]

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning(extracted)),
        patch("memory.tasks.consolidate_memory_task") as consolidate,
    ):
        cfg.get_config.return_value = _enabled_config()
        consolidate.aenqueue = AsyncMock()
        await extract_observations_task.func(str(activity.pk))

    rows = [obs async for obs in MemoryObservation.objects.filter(repo_id="group/project")]
    assert len(rows) == 2
    assert all(row.status == ObservationStatus.PENDING for row in rows)
    assert all(row.activity_id == activity.pk for row in rows)
    assert {row.category for row in rows} == {"build_test", "pitfall"}
    consolidate.aenqueue.assert_not_called()  # only 2 pending, below threshold


@pytest.mark.django_db(transaction=True)
async def test_extraction_skips_when_checkpoint_expired():
    activity = await _create_activity()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(None)),
        patch("memory.tasks._build_structured_llm") as build,
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(activity.pk))  # must not raise

    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_warns_when_checkpoint_has_no_messages(caplog):
    # A present checkpoint with an empty message list is a defect signature, distinct from
    # a missing/expired checkpoint: it skips like the expired case but logs at WARNING.
    activity = await _create_activity()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with([])),
        patch("memory.tasks._build_structured_llm") as build,
        caplog.at_level("WARNING", logger="daiv.memory"),
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(activity.pk))  # must not raise

    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0
    assert any("has no messages" in record.message for record in caplog.records)


@pytest.mark.django_db(transaction=True)
async def test_extraction_respects_daiv_yml_flag():
    activity = await _create_activity()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm") as build,
    ):
        cfg.get_config.return_value = _enabled_config(enabled=False)
        await extract_observations_task.func(str(activity.pk))

    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_handles_missing_activity():
    with patch("memory.tasks.RepositoryConfig") as cfg:
        await extract_observations_task.func("00000000-0000-0000-0000-000000000000")  # must not raise
        cfg.get_config.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_consolidation_triggered_at_threshold():
    activity = await _create_activity()
    for i in range(CONSOLIDATION_MIN_PENDING - 1):
        await MemoryObservation.objects.acreate(
            repo_id="group/project", category="codebase_fact", content=f"existing observation {i} with detail"
        )
    extracted = [ExtractedObservation(category="workflow", content="branch names must be kebab-case with prefix")]

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning(extracted)),
        patch("memory.tasks.consolidate_memory_task") as consolidate,
    ):
        cfg.get_config.return_value = _enabled_config()
        consolidate.aenqueue = AsyncMock()
        await extract_observations_task.func(str(activity.pk))

    consolidate.aenqueue.assert_awaited_once_with("group/project")


@pytest.mark.django_db(transaction=True)
async def test_consolidation_not_triggered_within_min_interval():
    from django.utils import timezone

    activity = await _create_activity()
    await RepositoryMemory.objects.acreate(repo_id="group/project", last_consolidated_at=timezone.now())
    for i in range(CONSOLIDATION_MIN_PENDING):
        await MemoryObservation.objects.acreate(
            repo_id="group/project", category="codebase_fact", content=f"existing observation {i} with detail"
        )

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning([])),
        patch("memory.tasks.consolidate_memory_task") as consolidate,
    ):
        cfg.get_config.return_value = _enabled_config()
        consolidate.aenqueue = AsyncMock()
        await extract_observations_task.func(str(activity.pk))

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_extraction_skips_activity_without_thread_id():
    activity = await _create_activity(thread_id=None)

    with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.tasks._build_structured_llm") as build:
        await extract_observations_task.func(str(activity.pk))  # must not raise

    cfg.get_config.assert_not_called()  # bails before loading config
    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0
