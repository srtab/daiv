from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

from django.core.management import CommandError, call_command

import pytest
from memory.consolidation import RoundOutcome, document_would_be_discarded
from memory.constants import CONSOLIDATION_MIN_PENDING
from memory.models import MemoryEntry, MemoryObservation, ObservationCategory, ObservationStatus, RepositoryMemory
from memory.schemas import MemoryOperation, MemoryOperations

from tests.unit_tests.memory.consolidation_helpers import _enabled_config, _site_settings, _structured_llm_returning


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

    @pytest.mark.django_db(transaction=True)
    def test_consolidation_really_runs_in_process(self):
        """The command's happy path, with only the model mocked rather than the whole task.

        The sibling tests patch ``consolidate_memory_task``, so they would still pass if ``.call()``
        returned an unawaited coroutine and consolidated nothing. This one asserts the round's
        effects, which is the only way to catch that.
        """
        observation = MemoryObservation.objects.create(
            repo_id="group/project",
            category=ObservationCategory.PITFALL,
            content="the sandbox caps command output at 2000 lines",
        )
        llm = _structured_llm_returning(
            MemoryOperation(
                op="ADD",
                category=ObservationCategory.PITFALL,
                content="the sandbox caps command output at 2000 lines",
                observation_ids=[str(observation.pk)],
            )
        )

        with (
            patch("memory.tasks.RepositoryConfig") as config,
            patch("memory.tasks.site_settings", _site_settings()),
            patch("memory.consolidation.build_structured_llm", return_value=llm),
            patch("memory.consolidation.site_settings", _site_settings()),
        ):
            config.get_config.return_value = _enabled_config()
            call_command("consolidate_memory", "--repo-id", "group/project", "--force", stdout=StringIO())

        observation.refresh_from_db()
        assert observation.status == ObservationStatus.CONSOLIDATED
        assert MemoryEntry.objects.filter(repo_id="group/project").active().count() == 1
        assert "2000 lines" in RepositoryMemory.objects.get(repo_id="group/project").content


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

    def test_reset_document_clears_a_document_no_replay_can_rebuild(self, caplog):
        # The wedge this flag exists for: a document the 0004 parse could not turn into entries and
        # with no unreplayed observations behind it. Consolidation refuses it forever until cleared.
        RepositoryMemory.objects.create(repo_id="group/stuck", content="## Pitfalls\n1. a numbered legacy bullet")

        with caplog.at_level("WARNING", logger="daiv.memory"):
            call_command("backfill_memory_entries", "--repo-id", "group/stuck", "--reset-document")

        memory = RepositoryMemory.objects.get(repo_id="group/stuck")
        assert memory.content == ""
        assert not document_would_be_discarded("group/stuck"), "the repo must be consolidatable again"
        assert "a numbered legacy bullet" in caplog.text, "the dropped text is only recoverable from this log"

    def test_reset_document_is_a_noop_without_a_stored_document(self, caplog):
        with caplog.at_level("INFO", logger="daiv.memory"):
            call_command("backfill_memory_entries", "--repo-id", "group/none", "--reset-document")

        assert "No stored memory document to reset" in caplog.text

    def test_reset_document_still_replays_what_it_can(self):
        RepositoryMemory.objects.create(repo_id="group/project", content="## Pitfalls\n- unparseable legacy")
        _create_consolidated("group/project", 2)

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig"),
            self._patched_round() as round_mock,
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/project", "--reset-document")

        assert RepositoryMemory.objects.get(repo_id="group/project").content == ""
        round_mock.assert_called_once()

    def test_a_replay_that_rebuilds_a_fraction_of_the_document_is_an_error(self, caplog):
        # The partial-backfill failure mode: the entries account for a fraction of the document they
        # were rebuilt from, and the live rounds will drop the rest on the next re-render.
        RepositoryMemory.objects.create(
            repo_id="group/project", content="## Codebase facts\n" + "\n".join(f"- legacy fact {n}" for n in range(40))
        )
        MemoryEntry.objects.create(
            repo_id="group/project", category=ObservationCategory.CODEBASE_FACT, content="the only one rebuilt"
        )
        _create_consolidated("group/project", 2)

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig"),
            self._patched_round(),
            caplog.at_level("ERROR", logger="daiv.memory"),
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/project")

        assert "rebuilt only" in caplog.text
        assert "not recoverable from the entries" in caplog.text

    @pytest.mark.django_db(transaction=True)
    def test_a_healthy_multi_batch_replay_is_not_reported_as_a_loss(self, caplog):
        """Every batch re-renders from the entries built so far, so a mid-replay document is short.

        Judging coverage per round reported the first batch of every healthy backfill as
        unrecoverable data loss — the rounds run unmocked here so that regression stays caught.
        """
        observations = _create_consolidated("group/project", 6)
        RepositoryMemory.objects.create(
            repo_id="group/project",
            content="## Codebase facts\n" + "\n".join(f"- historical observation {n}" for n in range(6)),
        )
        llm = MagicMock()
        llm.with_config.return_value.ainvoke = AsyncMock(
            side_effect=[
                MemoryOperations(
                    operations=[
                        MemoryOperation(
                            op="ADD",
                            category=ObservationCategory.CODEBASE_FACT,
                            content=observation.content,
                            observation_ids=[str(observation.pk)],
                        )
                        for observation in batch
                    ]
                )
                for batch in (observations[:2], observations[2:4], observations[4:])
            ]
        )

        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig") as config,
            patch("memory.consolidation.build_structured_llm", return_value=llm),
            patch("memory.consolidation.site_settings", _site_settings()),
            caplog.at_level("ERROR", logger="daiv.memory"),
        ):
            config.get_config.return_value = _enabled_config()
            call_command("backfill_memory_entries", "--repo-id", "group/project", "--batch-size", "2")

        assert MemoryEntry.objects.filter(repo_id="group/project").active().count() == 6
        assert caplog.text == ""

    def test_noop_when_nothing_to_replay(self, caplog):
        with (
            patch("memory.management.commands.backfill_memory_entries.RepositoryConfig") as config_mock,
            caplog.at_level("WARNING", logger="daiv.memory"),
        ):
            call_command("backfill_memory_entries", "--repo-id", "group/empty")

        config_mock.get_config.assert_not_called()
        assert "nothing to replay" in caplog.text

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
