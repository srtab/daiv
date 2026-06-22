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


def _structured_llm_returning(observations=None, *, error=None):
    llm = MagicMock()
    if error is not None:
        llm.with_config.return_value.ainvoke = AsyncMock(side_effect=error)
    else:
        llm.with_config.return_value.ainvoke = AsyncMock(
            return_value=ExtractedObservations(observations=observations or [])
        )
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


@pytest.mark.django_db(transaction=True)
async def test_consolidation_not_triggered_below_threshold():
    # One short of CONSOLIDATION_MIN_PENDING must NOT enqueue — guards the `<` vs `<=` boundary
    # (the existing test covers exactly-at-threshold; this covers exactly-below).
    activity = await _create_activity()
    for i in range(CONSOLIDATION_MIN_PENDING - 2):  # 8 existing + 1 extracted = 9 pending
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

    assert (
        await MemoryObservation.objects.filter(repo_id="group/project", status=ObservationStatus.PENDING).acount() == 9
    )
    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_consolidation_triggered_after_min_interval_elapsed():
    # >= threshold pending AND last consolidation older than the interval → must enqueue. Guards the
    # time-boundary in its firing direction (the existing test only covers within-interval suppression).
    from datetime import timedelta

    from django.utils import timezone

    activity = await _create_activity()
    await RepositoryMemory.objects.acreate(
        repo_id="group/project", last_consolidated_at=timezone.now() - timedelta(hours=25)
    )
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

    consolidate.aenqueue.assert_awaited_once_with("group/project")


@pytest.mark.django_db(transaction=True)
async def test_extraction_noop_when_model_spec_invalid():
    # A bad/unparseable extraction model spec raises ValueError; it must be swallowed (clean skip),
    # not crash the task. The hardcoded extraction models raise this in a deployment without the
    # OpenAI/Anthropic provider rows configured (regression guard for C1).
    activity = await _create_activity()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm", side_effect=ValueError("Unknown/Unsupported provider for model")),
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(activity.pk))  # must not raise

    assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_propagates_llm_failure_without_partial_writes():
    # The extraction ainvoke is deliberately unguarded: a transient/validation failure must propagate
    # (task FAILED, no retry — that run's signal is lost), write nothing partial, and not trigger
    # consolidation. Distinct from the model-misconfig precondition, which IS skipped silently.
    activity = await _create_activity()
    failing_llm = _structured_llm_returning(error=RuntimeError("upstream 500"))

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm", return_value=failing_llm),
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        pytest.raises(RuntimeError),
    ):
        cfg.get_config.return_value = _enabled_config()
        consolidate.aenqueue = AsyncMock()
        await extract_observations_task.func(str(activity.pk))

    assert await MemoryObservation.objects.acount() == 0
    consolidate.aenqueue.assert_not_called()
