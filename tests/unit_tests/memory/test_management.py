from io import StringIO
from unittest.mock import AsyncMock, patch

from django.core.management import CommandError, call_command

import pytest
from memory.constants import CONSOLIDATION_MIN_PENDING
from memory.models import MemoryEntry, MemoryObservation, ObservationCategory, ObservationStatus
from memory.tasks import RoundOutcome


def _create_pending(repo_id, n):
    for i in range(n):
        MemoryObservation.objects.create(
            repo_id=repo_id, category=ObservationCategory.CODEBASE_FACT, content=f"observation {i} with some detail"
        )


def _create_consolidated(repo_id, n):
    return [
        MemoryObservation.objects.create(
            repo_id=repo_id,
            category=ObservationCategory.CODEBASE_FACT,
            content=f"historical observation {i}",
            status=ObservationStatus.CONSOLIDATED,
        )
        for i in range(n)
    ]


@pytest.mark.django_db
class TestConsolidateMemoryCommand:
    def test_runs_consolidation_when_threshold_met(self):
        _create_pending("group/project", CONSOLIDATION_MIN_PENDING)
        out = StringIO()
        with patch("memory.management.commands.consolidate_memory.consolidate_memory_task") as task_mock:
            call_command("consolidate_memory", "--repo-id", "group/project", stdout=out)
        task_mock.call.assert_called_once_with("group/project")

    def test_below_threshold_requires_force(self):
        _create_pending("group/project", 2)
        with patch("memory.management.commands.consolidate_memory.consolidate_memory_task") as task_mock:
            with pytest.raises(CommandError, match="--force"):
                call_command("consolidate_memory", "--repo-id", "group/project")
            task_mock.call.assert_not_called()

            call_command("consolidate_memory", "--repo-id", "group/project", "--force", stdout=StringIO())
        task_mock.call.assert_called_once_with("group/project")

    def test_noop_when_nothing_pending(self, caplog):
        with (
            patch("memory.management.commands.consolidate_memory.consolidate_memory_task") as task_mock,
            caplog.at_level("WARNING", logger="daiv.memory"),
        ):
            call_command("consolidate_memory", "--repo-id", "group/empty")
        task_mock.call.assert_not_called()
        assert "No pending observations" in caplog.text


def _applied_outcome():
    """A round that applied one ADD — the shape the command's happy path reports on."""
    return RoundOutcome(applied=1, rejected=0, consolidated=1, discarded=0, still_pending=0)


@pytest.mark.django_db
class TestBackfillMemoryEntriesCommand:
    def _patched_round(self, outcome=None):
        return patch(
            "memory.management.commands.backfill_memory_entries.run_consolidation_round",
            new=AsyncMock(return_value=outcome or _applied_outcome()),
        )

    def test_replays_historical_observations_in_batches_oldest_first(self):
        observations = _create_consolidated("group/project", 5)

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig"),
            self._patched_round() as round_mock,
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/project", "--batch-size", "2")

        batches = [call.args[2] for call in round_mock.call_args_list]
        assert [len(batch) for batch in batches] == [2, 2, 1]
        assert [obs.pk for batch in batches for obs in batch] == [obs.pk for obs in observations]

    def test_skips_observations_already_linked_to_an_entry(self):
        replayed, unreplayed = _create_consolidated("group/project", 2)
        entry = MemoryEntry.objects.create(
            repo_id="group/project", category=ObservationCategory.CODEBASE_FACT, content="already built from it"
        )
        entry.observations.add(replayed)

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig"),
            self._patched_round() as round_mock,
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/project")

        assert [obs.pk for obs in round_mock.call_args.args[2]] == [unreplayed.pk]

    def test_ignores_pending_observations(self):
        # Pending observations belong to the live consolidation path, not the backfill.
        _create_pending("group/project", 3)

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig"),
            self._patched_round() as round_mock,
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/project")

        round_mock.assert_not_called()

    def test_noop_when_nothing_to_replay(self, caplog):
        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig") as config_mock,
            caplog.at_level("WARNING", logger="daiv.memory"),
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/empty")

        config_mock.get_config.assert_not_called()
        assert "nothing to do" in caplog.text

    def test_continues_after_a_batch_applies_nothing_then_fails(self, caplog):
        # A barren batch must not abort the ones after it, but the command must still exit
        # non-zero: an operator chaining off this repair action has to be able to see it.
        _create_consolidated("group/project", 3)

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig"),
            self._patched_round(outcome=None) as round_mock,
            caplog.at_level("WARNING", logger="daiv.memory"),
            pytest.raises(CommandError, match="2 of 3 batch"),
        ):
            round_mock.side_effect = [None, _applied_outcome(), None]
            call_command("backfill_memory_entries", "--repo-id", "group/project", "--batch-size", "1")

        assert round_mock.call_count == 3, "a barren batch must not abort the remaining ones"
        assert "remain unreplayed" in caplog.text

    def test_a_fully_applied_backfill_succeeds(self):
        _create_consolidated("group/project", 2)

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig"),
            self._patched_round(outcome=_applied_outcome()),
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/project", "--batch-size", "1")
