from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from memory.models import MemoryObservation, ObservationCategory, ObservationStatus, RepositoryMemory
from memory.tasks import CONSOLIDATION_MAX_PENDING_AGE_DAYS, CONSOLIDATION_MIN_PENDING, consolidate_memory_cron_task


def _site_settings(**overrides):
    """Mock of the site-settings singleton with the memory defaults the cron reads."""
    ss = MagicMock()
    ss.memory_enabled = True
    ss.memory_consolidation_min_pending = CONSOLIDATION_MIN_PENDING
    ss.memory_consolidation_max_pending_age_days = CONSOLIDATION_MAX_PENDING_AGE_DAYS
    ss.memory_consolidation_min_interval_hours = 24
    for key, value in overrides.items():
        setattr(ss, key, value)
    return ss


async def _create_observations(repo_id, n, *, status=ObservationStatus.PENDING, age_days=0):
    for i in range(n):
        observation = await MemoryObservation.objects.acreate(
            repo_id=repo_id, category=ObservationCategory.PITFALL, content=f"observation {i} with detail", status=status
        )
        if age_days:
            # created_at is auto_now_add, so backdating needs a second write.
            await MemoryObservation.objects.filter(pk=observation.pk).aupdate(
                created_at=timezone.now() - timedelta(days=age_days)
            )


@pytest.mark.django_db(transaction=True)
async def test_cron_enqueues_repo_at_threshold():
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_awaited_once_with("group/project")


@pytest.mark.django_db(transaction=True)
async def test_cron_skips_repo_below_threshold():
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING - 1)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_cron_skips_repo_within_min_interval():
    await RepositoryMemory.objects.acreate(repo_id="group/project", last_attempted_at=timezone.now())
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_cron_enqueues_after_min_interval_elapsed():
    await RepositoryMemory.objects.acreate(
        repo_id="group/project", last_attempted_at=timezone.now() - timedelta(hours=25)
    )
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_awaited_once_with("group/project")


@pytest.mark.django_db(transaction=True)
async def test_cron_enqueues_when_memory_never_attempted():
    # A RepositoryMemory row exists but last_attempted_at is None (never attempted) → must enqueue.
    await RepositoryMemory.objects.acreate(repo_id="group/project", last_attempted_at=None)
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_awaited_once_with("group/project")


@pytest.mark.django_db(transaction=True)
async def test_cron_noop_when_site_disabled():
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings(memory_enabled=False)),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_cron_sweeps_multiple_repos_independently():
    # The core gain over the old extraction-time trigger: a single pass enqueues every eligible
    # repo (including ones that have gone quiet) and skips the under-threshold one.
    await _create_observations("group/alpha", CONSOLIDATION_MIN_PENDING)
    await _create_observations("group/beta", CONSOLIDATION_MIN_PENDING)
    await _create_observations("group/under", CONSOLIDATION_MIN_PENDING - 1)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    enqueued = {call.args[0] for call in consolidate.aenqueue.await_args_list}
    assert enqueued == {"group/alpha", "group/beta"}


@pytest.mark.django_db(transaction=True)
async def test_cron_continues_after_per_repo_enqueue_failure(caplog):
    # A per-repo enqueue error must not abort the sweep: the remaining eligible repos are still
    # enqueued, and the failure is logged rather than swallowed.
    for repo_id in ("group/alpha", "group/beta", "group/gamma"):
        await _create_observations(repo_id, CONSOLIDATION_MIN_PENDING)

    async def _flaky_aenqueue(repo_id):
        if repo_id == "group/beta":
            raise RuntimeError("broker unavailable")

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
        caplog.at_level("WARNING", logger="daiv.memory"),
    ):
        consolidate.aenqueue = AsyncMock(side_effect=_flaky_aenqueue)
        await consolidate_memory_cron_task.func()  # must not raise

    attempted = {call.args[0] for call in consolidate.aenqueue.await_args_list}
    assert attempted == {"group/alpha", "group/beta", "group/gamma"}  # the failure did not stop the sweep
    assert "1 repo(s) failed to enqueue" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_cron_counts_only_pending_observations():
    # Already-consolidated rows must not count toward the threshold.
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING, status=ObservationStatus.CONSOLIDATED)
    await _create_observations("group/project", 2)  # only 2 pending

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_cron_age_flush_ignores_already_consolidated_history():
    # oldest_pending must be scoped to PENDING rows like the count is. If old CONSOLIDATED
    # history leaked into Min(created_at), every long-lived repo would be permanently due.
    await _create_observations("group/project", 3, status=ObservationStatus.CONSOLIDATED, age_days=400)
    await _create_observations("group/project", 2)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_cron_threshold_is_configurable():
    # Threshold lowered to 3: three pending observations must enqueue where the default of 10 would not.
    await _create_observations("group/project", 3)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings(memory_consolidation_min_pending=3)),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_awaited_once_with("group/project")


@pytest.mark.django_db(transaction=True)
async def test_cron_enqueues_low_volume_repo_once_its_oldest_observation_ages_past_the_flush():
    # Below the count gate forever otherwise: a repo with two observations must still consolidate
    # once they age past the flush threshold, or it never forms memory at all.
    await _create_observations("group/quiet", 2, age_days=CONSOLIDATION_MAX_PENDING_AGE_DAYS + 1)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_awaited_once_with("group/quiet")


@pytest.mark.django_db(transaction=True)
async def test_cron_skips_repo_below_threshold_with_young_observations():
    await _create_observations("group/quiet", 2, age_days=CONSOLIDATION_MAX_PENDING_AGE_DAYS - 1)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_cron_age_flush_still_respects_the_min_interval():
    # The age trigger widens what is due; it does not bypass the cooldown.
    await RepositoryMemory.objects.acreate(repo_id="group/quiet", last_attempted_at=timezone.now())
    await _create_observations("group/quiet", 2, age_days=CONSOLIDATION_MAX_PENDING_AGE_DAYS + 1)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_cron_flush_age_is_configurable():
    await _create_observations("group/quiet", 1, age_days=2)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings(memory_consolidation_max_pending_age_days=1)),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_awaited_once_with("group/quiet")


@pytest.mark.django_db(transaction=True)
async def test_cron_interval_is_configurable():
    # Last consolidation was 2h ago: the default 24h interval would suppress, but lowered to 1h it must enqueue.
    await RepositoryMemory.objects.acreate(
        repo_id="group/project", last_attempted_at=timezone.now() - timedelta(hours=2)
    )
    await _create_observations("group/project", CONSOLIDATION_MIN_PENDING)

    with (
        patch("memory.tasks.consolidate_memory_task") as consolidate,
        patch("memory.tasks.site_settings", _site_settings(memory_consolidation_min_interval_hours=1)),
    ):
        consolidate.aenqueue = AsyncMock()
        await consolidate_memory_cron_task.func()

    consolidate.aenqueue.assert_awaited_once_with("group/project")
