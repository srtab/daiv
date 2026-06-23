from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from langchain_core.messages import AIMessage, HumanMessage
from memory.models import MemoryObservation, ObservationStatus
from memory.schemas import ExtractedObservation, ExtractedObservations
from memory.tasks import extract_observations_task


def _enabled_config(enabled=True):
    config = MagicMock()
    config.memory.enabled = enabled
    return config


def _site_settings(**overrides):
    """Mock of the site-settings singleton with the memory defaults the task reads."""
    ss = MagicMock()
    ss.memory_enabled = True
    ss.memory_extraction_model_name = "openrouter:openai/gpt-5.4-mini"
    ss.memory_extraction_fallback_model_name = "openrouter:anthropic/claude-haiku-4.5"
    for key, value in overrides.items():
        setattr(ss, key, value)
    return ss


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
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(activity.pk))

    rows = [obs async for obs in MemoryObservation.objects.filter(repo_id="group/project")]
    assert len(rows) == 2
    assert all(row.status == ObservationStatus.PENDING for row in rows)
    assert all(row.activity_id == activity.pk for row in rows)
    assert {row.category for row in rows} == {"build_test", "pitfall"}


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
@pytest.mark.parametrize(
    ("fallback_model", "expected_models"),
    [
        # Both configured → both passed through.
        ("provider:m2", ("provider:m1", "provider:m2")),
        # Empty fallback is filtered out so _build_structured_llm gets a 1-tuple, not (model, None).
        (None, ("provider:m1",)),
    ],
    ids=["with_fallback", "drops_empty_fallback"],
)
async def test_extraction_uses_configured_models(fallback_model, expected_models):
    activity = await _create_activity()
    extracted = [ExtractedObservation(category="build_test", content="`make test` needs the DB up first")]

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning(extracted)) as build,
        patch(
            "memory.tasks.site_settings",
            _site_settings(
                memory_extraction_model_name="provider:m1", memory_extraction_fallback_model_name=fallback_model
            ),
        ),
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(activity.pk))

    _schema, models = build.call_args.args
    assert tuple(models) == expected_models


@pytest.mark.django_db(transaction=True)
async def test_extraction_noop_when_no_model_configured():
    # Both model and fallback empty (only reachable via an empty-string env override) → clean skip,
    # not an IndexError crash in _build_structured_llm.
    activity = await _create_activity()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm") as build,
        patch(
            "memory.tasks.site_settings",
            _site_settings(memory_extraction_model_name="", memory_extraction_fallback_model_name=""),
        ),
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(activity.pk))  # must not raise

    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_noop_when_site_disabled():
    # Repo flag is on, but the instance-wide master switch is off → must not run.
    activity = await _create_activity()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm") as build,
        patch("memory.tasks.site_settings", _site_settings(memory_enabled=False)),
    ):
        cfg.get_config.return_value = _enabled_config(enabled=True)
        await extract_observations_task.func(str(activity.pk))

    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_handles_missing_activity():
    with patch("memory.tasks.RepositoryConfig") as cfg:
        await extract_observations_task.func("00000000-0000-0000-0000-000000000000")  # must not raise
        cfg.get_config.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_extraction_skips_activity_without_thread_id():
    activity = await _create_activity(thread_id=None)

    with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.tasks._build_structured_llm") as build:
        await extract_observations_task.func(str(activity.pk))  # must not raise

    cfg.get_config.assert_not_called()  # bails before loading config
    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0


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
    # (task FAILED, no retry — that run's signal is lost) and write nothing partial. Distinct from the
    # model-misconfig precondition, which IS skipped silently.
    activity = await _create_activity()
    failing_llm = _structured_llm_returning(error=RuntimeError("upstream 500"))

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.tasks._build_structured_llm", return_value=failing_llm),
        pytest.raises(RuntimeError),
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(activity.pk))

    assert await MemoryObservation.objects.acount() == 0
