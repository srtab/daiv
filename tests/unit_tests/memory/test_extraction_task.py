from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memory.models import MemoryObservation, ObservationStatus
from memory.schemas import ExtractedObservation
from memory.tasks import extract_observations_task

from tests.unit_tests.memory.extraction_helpers import (
    TRANSCRIPT,
    _checkpointer_with,
    _create_run,
    _enabled_config,
    _site_settings,
    _structured_llm_returning,
)


@pytest.mark.django_db(transaction=True)
async def test_extraction_creates_observation_rows():
    run = await _create_run()
    extracted = [
        ExtractedObservation(category="build_test", content="`make test` needs LANGCHAIN_TRACING_V2=false set"),
        ExtractedObservation(category="pitfall", content="editing pyproject.toml directly breaks uv lock sync"),
    ]

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm", return_value=_structured_llm_returning(extracted)),
        patch("memory.extraction.site_settings", _site_settings()),
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(run.pk))

    rows = [obs async for obs in MemoryObservation.objects.filter(repo_id="group/project")]
    assert len(rows) == 2
    assert all(row.status == ObservationStatus.PENDING for row in rows)
    assert all(row.run_id == run.pk for row in rows)
    assert {row.category for row in rows} == {"build_test", "pitfall"}


@pytest.mark.django_db(transaction=True)
async def test_extraction_respects_daiv_yml_flag():
    run = await _create_run()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm") as build,
        patch("memory.extraction.site_settings", _site_settings()),
    ):
        cfg.get_config.return_value = _enabled_config(enabled=False)
        await extract_observations_task.func(str(run.pk))

    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_noop_when_site_disabled():
    # Repo flag is on, but the instance-wide master switch is off → must not run.
    run = await _create_run()

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm") as build,
        patch("memory.tasks.site_settings", _site_settings(memory_enabled=False)),
    ):
        cfg.get_config.return_value = _enabled_config(enabled=True)
        await extract_observations_task.func(str(run.pk))

    build.assert_not_called()
    assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_handles_missing_run():
    with patch("memory.tasks.RepositoryConfig") as cfg:
        await extract_observations_task.func("00000000-0000-0000-0000-000000000000")  # must not raise
        cfg.get_config.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_extraction_skips_run_without_session_id():
    run = await _create_run()
    # Patch the queryset to return a run with no session_id.
    # Run is imported locally inside extract_observations_task, so patch via sessions.models.
    run.session_id = None

    with patch("sessions.models.Run") as mock_run:
        mock_qs = MagicMock()
        mock_qs.afirst = AsyncMock(return_value=run)
        mock_run.objects.filter.return_value = mock_qs

        with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.extraction.build_structured_llm") as build:
            await extract_observations_task.func(str(run.pk))  # must not raise

        cfg.get_config.assert_not_called()  # bails before loading config
        build.assert_not_called()
        assert await MemoryObservation.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_extraction_propagates_llm_failure_without_partial_writes():
    # The extraction ainvoke is deliberately unguarded: a transient/validation failure must propagate
    # (task FAILED, no retry — that run's signal is lost) and write nothing partial. Distinct from the
    # model-misconfig precondition, which IS skipped silently.
    run = await _create_run()
    failing_llm = _structured_llm_returning(error=RuntimeError("upstream 500"))

    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm", return_value=failing_llm),
        patch("memory.extraction.site_settings", _site_settings()),
        pytest.raises(RuntimeError),
    ):
        cfg.get_config.return_value = _enabled_config()
        await extract_observations_task.func(str(run.pk))

    assert await MemoryObservation.objects.acount() == 0
