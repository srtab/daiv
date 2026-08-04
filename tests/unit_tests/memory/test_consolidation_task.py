from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from memory.models import (
    EntryStatus,
    MemoryEntry,
    MemoryObservation,
    ObservationCategory,
    ObservationStatus,
    RepositoryMemory,
)
from memory.schemas import MemoryOperation, MemoryOperations
from memory.tasks import CONSOLIDATION_MIN_PENDING, MEMORY_MAX_BYTES, MEMORY_MAX_LINES, consolidate_memory_task


def _enabled_config(enabled=True):
    config = MagicMock()
    config.memory.enabled = enabled
    config.models.agent.model = "openrouter:anthropic/claude-sonnet-4.6"
    config.models.agent.fallback_model = "openrouter:openai/gpt-5.3-codex"
    return config


def _site_settings(**overrides):
    """Mock of the site-settings singleton with the memory defaults the task reads."""
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


async def _run_round(llm, config=None):
    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks._build_structured_llm", return_value=llm),
        patch("memory.tasks.site_settings", _site_settings()),
    ):
        cfg.get_config.return_value = config or _enabled_config()
        await consolidate_memory_task.func("group/project")


@pytest.mark.django_db(transaction=True)
class TestOperations:
    async def test_add_creates_an_entry_and_renders_it(self):
        obs = await _observation(category=ObservationCategory.BUILD_TEST, content="`make test` needs -n auto")
        llm = _structured_llm_returning(
            MemoryOperation(
                op="ADD",
                observation_ids=[str(obs.pk)],
                category="build_test",
                content="`make test` runs the suite with -n auto",
            )
        )

        await _run_round(llm)

        entry = await MemoryEntry.objects.aget(repo_id="group/project")
        assert entry.status == EntryStatus.ACTIVE
        assert entry.content == "`make test` runs the suite with -n auto"
        assert entry.category == ObservationCategory.BUILD_TEST
        assert [o.pk async for o in entry.observations.all()] == [obs.pk]

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Build & test\n- `make test` runs the suite with -n auto"
        assert memory.last_consolidated_at is not None
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.CONSOLIDATED

    async def test_entries_no_operation_names_stay_in_the_document(self):
        await _entry("an untouched workflow rule", category=ObservationCategory.WORKFLOW)
        obs = await _observation(content="something new about pitfalls")
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a brand new pitfall")
        )

        await _run_round(llm)

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- a brand new pitfall\n\n## Workflow\n- an untouched workflow rule"

    async def test_new_entry_records_the_run_that_taught_it(self):
        from sessions.models import Run, RunStatus, Session, SessionOrigin

        session = await Session.objects.acreate(thread_id="t1", origin=SessionOrigin.API_JOB, repo_id="group/project")
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id="group/project", status=RunStatus.SUCCESSFUL
        )
        obs = await MemoryObservation.objects.acreate(
            repo_id="group/project", run=run, category=ObservationCategory.PITFALL, content="learned the hard way"
        )
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a fact with a source")
        )

        await _run_round(llm)

        entry = await MemoryEntry.objects.aget()
        assert entry.source_run_id == run.pk

    async def test_new_entry_credits_the_newest_source_run(self):
        # The model lists observation ids in whatever order it likes, so provenance has to be
        # decided by created_at — and a run-less newest observation must not blank it.
        from sessions.models import Run, RunStatus, Session, SessionOrigin

        session = await Session.objects.acreate(thread_id="t1", origin=SessionOrigin.API_JOB, repo_id="group/project")
        runs = [
            await Run.objects.acreate(
                session=session,
                trigger_type=SessionOrigin.API_JOB,
                repo_id="group/project",
                status=RunStatus.SUCCESSFUL,
            )
            for _ in range(2)
        ]
        older = await MemoryObservation.objects.acreate(
            repo_id="group/project", run=runs[0], category=ObservationCategory.PITFALL, content="the older lesson"
        )
        newer = await MemoryObservation.objects.acreate(
            repo_id="group/project", run=runs[1], category=ObservationCategory.PITFALL, content="the newer lesson"
        )
        runless = await MemoryObservation.objects.acreate(
            repo_id="group/project", category=ObservationCategory.PITFALL, content="the newest, with no run"
        )
        llm = _structured_llm_returning(
            MemoryOperation(
                op="ADD",
                # Oldest first, so a scan in list order would credit the wrong run.
                observation_ids=[str(older.pk), str(newer.pk), str(runless.pk)],
                category="pitfall",
                content="a fact with several sources",
            )
        )

        await _run_round(llm)

        entry = await MemoryEntry.objects.aget()
        assert entry.source_run_id == runs[1].pk

    async def test_update_supersedes_exactly_the_named_entry(self):
        stale = await _entry("settings live in daiv/settings/components/")
        obs = await _observation(content="settings actually live in daiv/daiv/settings/components/")
        llm = _structured_llm_returning(
            MemoryOperation(
                op="UPDATE",
                entry_ids=[str(stale.pk)],
                observation_ids=[str(obs.pk)],
                content="settings live in daiv/daiv/settings/components/",
            )
        )

        await _run_round(llm)

        await stale.arefresh_from_db()
        successor = await MemoryEntry.objects.filter(status=EntryStatus.ACTIVE).aget()
        assert stale.status == EntryStatus.SUPERSEDED
        assert stale.superseded_by_id == successor.pk
        assert stale.content == "settings live in daiv/settings/components/", "history stays verbatim"
        assert successor.content == "settings live in daiv/daiv/settings/components/"
        assert successor.category == stale.category, "the successor inherits the superseded entry's category"

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- settings live in daiv/daiv/settings/components/", (
            "the superseded entry must be gone from the document, not sitting next to its correction"
        )

    async def test_merge_supersedes_every_named_entry_into_one(self):
        first = await _entry("sandbox timeout is 600s")
        second = await _entry("make test exceeds the sandbox timeout")
        obs = await _observation(content="prefer targeted pytest runs inside the sandbox")
        llm = _structured_llm_returning(
            MemoryOperation(
                op="MERGE",
                entry_ids=[str(first.pk), str(second.pk)],
                observation_ids=[str(obs.pk)],
                category="pitfall",
                content="the sandbox times out at 600s, so run targeted pytest instead of the full suite",
            )
        )

        await _run_round(llm)

        successor = await MemoryEntry.objects.filter(status=EntryStatus.ACTIVE).aget()
        for source in (first, second):
            await source.arefresh_from_db()
            assert source.status == EntryStatus.SUPERSEDED
            assert source.superseded_by_id == successor.pk
        assert await MemoryEntry.objects.acount() == 3

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == (
            "## Pitfalls\n- the sandbox times out at 600s, so run targeted pytest instead of the full suite"
        ), "both merged fragments must be gone from the document"

    async def test_confirm_bumps_the_timestamp_without_creating_an_entry(self):
        entry = await _entry("never edit pyproject.toml by hand")
        obs = await _observation(content="editing pyproject.toml by hand broke the uv lock again")
        llm = _structured_llm_returning(
            MemoryOperation(op="CONFIRM", entry_ids=[str(entry.pk)], observation_ids=[str(obs.pk)])
        )

        await _run_round(llm)

        confirmed = await MemoryEntry.objects.aget(pk=entry.pk)
        assert confirmed.status == EntryStatus.ACTIVE
        assert confirmed.last_confirmed_at > entry.last_confirmed_at
        assert confirmed.content == entry.content
        assert await MemoryEntry.objects.acount() == 1
        # The confirming observation is kept as provenance — it is also what makes the backfill idempotent.
        assert [o.pk async for o in confirmed.observations.all()] == [obs.pk]
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.CONSOLIDATED

    async def test_discard_marks_the_observation_discarded(self):
        obs = await _observation(content="the pipeline had 3 failures this morning")
        llm = _structured_llm_returning(
            MemoryOperation(op="DISCARD", observation_ids=[str(obs.pk)], reason="one-off run state")
        )

        await _run_round(llm)

        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.DISCARDED
        assert not await MemoryEntry.objects.aexists()


@pytest.mark.django_db(transaction=True)
class TestBudgetPressure:
    async def test_a_round_that_overflows_the_budget_supersedes_the_stalest_entry(self):
        stale = await MemoryEntry.objects.acreate(
            repo_id="group/project",
            category=ObservationCategory.PITFALL,
            content="the stalest fact " + "x" * 60,
            last_confirmed_at=timezone.now() - timedelta(days=90),
        )
        fresh = await MemoryEntry.objects.acreate(
            repo_id="group/project", category=ObservationCategory.PITFALL, content="a freshly confirmed fact"
        )
        obs = await _observation(content="one more fact than the budget allows")
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="the newest fact")
        )

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.tasks._build_structured_llm", return_value=llm),
            patch("memory.tasks.site_settings", _site_settings(memory_max_bytes=120)),
        ):
            cfg.get_config.return_value = _enabled_config()
            await consolidate_memory_task.func("group/project")

        await stale.arefresh_from_db()
        await fresh.arefresh_from_db()
        assert stale.status == EntryStatus.SUPERSEDED, "eviction supersedes rather than deletes"
        assert stale.superseded_by is None, "an evicted entry has no successor"
        assert stale.content.startswith("the stalest fact"), "the evicted entry stays recoverable verbatim"
        assert fresh.status == EntryStatus.ACTIVE
        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert len(memory.content.encode("utf-8")) <= 120


@pytest.mark.django_db(transaction=True)
class TestValidation:
    async def test_untouched_entries_are_byte_identical_after_a_round(self):
        untouched = [await _entry(f"fact number {i} worth keeping") for i in range(5)]
        targeted = await _entry("fact to be corrected")
        obs = await _observation(content="the corrected version of that fact")
        llm = _structured_llm_returning(
            MemoryOperation(
                op="UPDATE", entry_ids=[str(targeted.pk)], observation_ids=[str(obs.pk)], content="corrected fact"
            )
        )

        before = {entry.pk: (entry.content, entry.status, entry.last_confirmed_at) for entry in untouched}

        await _run_round(llm)

        after = {
            entry.pk: (entry.content, entry.status, entry.last_confirmed_at)
            async for entry in MemoryEntry.objects.filter(pk__in=list(before))
        }
        assert after == before

    @pytest.mark.parametrize(
        ("operation_kwargs", "reason"),
        [
            ({"op": "UPDATE", "entry_ids": ["00000000-0000-0000-0000-000000000000"], "content": "x"}, "unknown entry"),
            ({"op": "UPDATE", "entry_ids": ["not-even-a-uuid"], "content": "x"}, "hallucinated id"),
            ({"op": "ADD", "category": "pitfall"}, "no content"),
            ({"op": "ADD", "content": "x", "category": None}, "no category"),
            ({"op": "UPDATE", "content": "x"}, "no entry named"),
            ({"op": "CONFIRM"}, "no entry named"),
            ({"op": "ADD", "category": "pitfall", "content": "   \n  "}, "whitespace-only content"),
            ({"op": "DISCARD"}, "no reason given"),
            ({"op": "DISCARD", "reason": "  "}, "whitespace-only reason"),
        ],
    )
    async def test_malformed_operation_is_rejected(self, operation_kwargs, reason):
        obs = await _observation()
        llm = _structured_llm_returning(MemoryOperation(observation_ids=[str(obs.pk)], **operation_kwargs))

        await _run_round(llm)

        assert not await MemoryEntry.objects.aexists(), reason
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING, "a rejected operation's observations stay pending"

    @pytest.mark.parametrize(
        "build_operation",
        [
            lambda ids: {"op": "ADD", "entry_ids": ids[:1], "category": "pitfall", "content": "x"},
            lambda ids: {"op": "DISCARD", "entry_ids": ids[:1], "reason": "wrong op shape"},
            lambda ids: {"op": "UPDATE", "entry_ids": ids[:1]},
            lambda ids: {"op": "MERGE", "entry_ids": ids, "category": "pitfall"},
            lambda ids: {"op": "MERGE", "entry_ids": ids[:1], "category": "pitfall", "content": "x"},
            # An UPDATE over several entries would be a MERGE with no same-category fence.
            lambda ids: {"op": "UPDATE", "entry_ids": ids, "content": "x"},
            lambda ids: {"op": "CONFIRM", "entry_ids": ids},
            # Deduplication must happen before the arity check, or a repeated id passes "two or
            # more", supersedes one entry twice and orphans a successor link.
            lambda ids: {"op": "MERGE", "entry_ids": [ids[0], ids[0]], "category": "pitfall", "content": "x"},
        ],
        ids=[
            "add-naming-entry",
            "discard-naming-entry",
            "update-without-content",
            "merge-without-content",
            "merge-of-one",
            "update-over-many-entries",
            "confirm-over-many-entries",
            "merge-of-one-repeated-twice",
        ],
    )
    async def test_operation_shape_is_fenced_per_op(self, build_operation):
        entries = [await _entry(f"existing fact {i}") for i in range(2)]
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(observation_ids=[str(obs.pk)], **build_operation([str(entry.pk) for entry in entries]))
        )

        await _run_round(llm)

        for entry in entries:
            await entry.arefresh_from_db()
            assert entry.status == EntryStatus.ACTIVE
        assert await MemoryEntry.objects.acount() == 2, "no entry created or retired by a malformed operation"
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    async def test_operation_without_observations_is_rejected(self):
        # Every operation must be attributable to the observations that motivated it.
        entry = await _entry("an existing fact")
        await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="UPDATE", entry_ids=[str(entry.pk)], content="an unattributed rewrite")
        )

        await _run_round(llm)

        await entry.arefresh_from_db()
        assert entry.status == EntryStatus.ACTIVE
        assert entry.content == "an existing fact"

    async def test_superseded_entry_cannot_be_targeted(self):
        retired = await _entry("already retired")
        retired.status = EntryStatus.SUPERSEDED
        await retired.asave(update_fields=["status"])
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(
                op="UPDATE", entry_ids=[str(retired.pk)], observation_ids=[str(obs.pk)], content="resurrection attempt"
            )
        )

        await _run_round(llm)

        assert await MemoryEntry.objects.filter(status=EntryStatus.ACTIVE).acount() == 0
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    async def test_cross_category_merge_is_rejected(self):
        pitfall = await _entry("a pitfall", category=ObservationCategory.PITFALL)
        workflow = await _entry("a workflow rule", category=ObservationCategory.WORKFLOW)
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(
                op="MERGE",
                entry_ids=[str(pitfall.pk), str(workflow.pk)],
                observation_ids=[str(obs.pk)],
                category="pitfall",
                content="an illegal cross-category merge",
            )
        )

        await _run_round(llm)

        for entry in (pitfall, workflow):
            await entry.arefresh_from_db()
            assert entry.status == EntryStatus.ACTIVE
        assert await MemoryEntry.objects.acount() == 2

    async def test_out_of_round_observation_is_rejected(self):
        in_round = await _observation(content="an observation from this round")
        already_consolidated = await MemoryObservation.objects.acreate(
            repo_id="group/project",
            category=ObservationCategory.PITFALL,
            content="an observation from a past round",
            status=ObservationStatus.CONSOLIDATED,
        )
        llm = _structured_llm_returning(
            MemoryOperation(
                op="ADD",
                observation_ids=[str(already_consolidated.pk)],
                category="pitfall",
                content="built on an observation outside the round",
            ),
            MemoryOperation(
                op="ADD", observation_ids=[str(in_round.pk)], category="pitfall", content="a legitimately new fact"
            ),
        )

        await _run_round(llm)

        entry = await MemoryEntry.objects.aget()
        assert entry.content == "a legitimately new fact", "the valid remainder still applies"
        await already_consolidated.arefresh_from_db()
        assert already_consolidated.status == ObservationStatus.CONSOLIDATED, "untouched by the rejected operation"

    async def test_observation_claimed_twice_only_counts_once(self):
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="the first claim"),
            MemoryOperation(op="DISCARD", observation_ids=[str(obs.pk)], reason="contradictory second claim"),
        )

        await _run_round(llm)

        assert await MemoryEntry.objects.acount() == 1
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.CONSOLIDATED, "the first operation wins, the contradiction is dropped"

    async def test_entry_targeted_twice_is_only_changed_once(self):
        # Superseding the same entry twice in one round would orphan one of the successor links.
        entry = await _entry("the original fact")
        first_obs = await _observation(content="the first correction")
        second_obs = await _observation(content="a second, conflicting correction")
        llm = _structured_llm_returning(
            MemoryOperation(
                op="UPDATE", entry_ids=[str(entry.pk)], observation_ids=[str(first_obs.pk)], content="first successor"
            ),
            MemoryOperation(
                op="UPDATE", entry_ids=[str(entry.pk)], observation_ids=[str(second_obs.pk)], content="second successor"
            ),
        )

        await _run_round(llm)

        await entry.arefresh_from_db()
        successor = await MemoryEntry.objects.filter(status=EntryStatus.ACTIVE).aget()
        assert successor.content == "first successor"
        assert entry.superseded_by_id == successor.pk
        await second_obs.arefresh_from_db()
        assert second_obs.status == ObservationStatus.PENDING, "the dropped operation's observation is re-queued"

    async def test_no_operations_leaves_memory_untouched(self):
        await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- keep me")
        obs = await _observation()

        await _run_round(_structured_llm_returning())

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- keep me"
        assert memory.last_consolidated_at is None
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    async def test_round_with_only_invalid_operations_writes_nothing(self):
        await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- keep me")
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="UPDATE", entry_ids=["nope"], observation_ids=[str(obs.pk)], content="x")
        )

        await _run_round(llm)

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- keep me"
        assert memory.last_consolidated_at is None
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    async def test_round_is_atomic_when_the_document_write_fails(self):
        # Entry writes, status flips and the re-render commit together: a failure on the last
        # statement of the block must roll all of them back, never half-apply.
        from django.db import DatabaseError

        await _entry("prior fact")
        await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- prior fact")
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a brand new fact")
        )

        with (
            patch("memory.models.RepositoryMemory.save", side_effect=DatabaseError("write failed")),
            pytest.raises(DatabaseError),
        ):
            await _run_round(llm)

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- prior fact"
        assert memory.last_consolidated_at is None
        assert await MemoryEntry.objects.acount() == 1, "the ADD must have rolled back"
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING


@pytest.mark.django_db(transaction=True)
class TestDegradedRoundReporting:
    async def test_observations_the_model_never_named_are_reported_as_an_error(self, caplog):
        # These stay pending and are retried forever if the model keeps skipping them, so the
        # signal has to be an error — warnings never reach Sentry.
        claimed = await _observation(content="the one the model handled")
        ignored = await _observation(content="the one the model forgot")
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(claimed.pk)], category="pitfall", content="a new fact")
        )

        with caplog.at_level("ERROR", logger="daiv.memory"):
            await _run_round(llm)

        assert "left 1 of 2 observation(s) unclaimed" in caplog.text
        await ignored.arefresh_from_db()
        assert ignored.status == ObservationStatus.PENDING

    async def test_mostly_rejected_round_is_reported_as_an_error(self, caplog):
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="the only good one"),
            MemoryOperation(op="UPDATE", entry_ids=["not-a-real-id"], observation_ids=[str(obs.pk)], content="x"),
            MemoryOperation(op="MERGE", entry_ids=["nope"], observation_ids=[str(obs.pk)], content="y"),
        )

        with caplog.at_level("ERROR", logger="daiv.memory"):
            await _run_round(llm)

        assert "rejected 2 of 3 operations" in caplog.text

    async def test_healthy_round_reports_no_error(self, caplog):
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a new fact")
        )

        with caplog.at_level("ERROR", logger="daiv.memory"):
            await _run_round(llm)

        assert caplog.text == ""


@pytest.mark.django_db(transaction=True)
class TestLegacyDocumentGuard:
    async def test_round_refuses_to_overwrite_a_document_that_has_no_entries(self):
        # A repo consolidated before entries existed would lose its whole document to the
        # re-render, so the round bails out before the LLM call and stays retryable.
        await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- a legacy fact")
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a brand new fact")
        )

        await _run_round(llm)

        llm.with_config.return_value.ainvoke.assert_not_awaited()
        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- a legacy fact"
        assert not await MemoryEntry.objects.aexists()
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    async def test_round_proceeds_once_the_document_has_entries_behind_it(self):
        await _entry("a legacy fact")
        await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- a legacy fact")
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a brand new fact")
        )

        await _run_round(llm)

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- a legacy fact\n- a brand new fact"


@pytest.mark.django_db(transaction=True)
class TestPreconditionsAndPrompt:
    async def test_noop_when_disabled_or_nothing_pending(self):
        with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.tasks._build_structured_llm") as build:
            cfg.get_config.return_value = _enabled_config(enabled=False)
            await consolidate_memory_task.func("group/project")
            build.assert_not_called()

        with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.tasks._build_structured_llm") as build:
            cfg.get_config.return_value = _enabled_config()
            await consolidate_memory_task.func("group/empty-repo")
            build.assert_not_called()
        assert not await RepositoryMemory.objects.filter(repo_id="group/empty-repo").aexists()

    async def test_noop_when_site_disabled(self):
        obs = await _observation()

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.tasks._build_structured_llm") as build,
            patch("memory.tasks.site_settings", _site_settings(memory_enabled=False)),
        ):
            cfg.get_config.return_value = _enabled_config(enabled=True)
            await consolidate_memory_task.func("group/project")

        build.assert_not_called()
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("no api key"), ValueError("Unknown/Unsupported provider")],
        ids=["provider-unconfigured", "model-spec-invalid"],
    )
    async def test_noop_when_model_unavailable(self, exc):
        # _build_structured_llm raising must skip, not crash: nothing consolidated, observations
        # stay pending. RuntimeError = provider disabled / no API key; ValueError = bad/unparseable
        # spec from parse_model_spec (regression guard for C1, whose original guard caught only
        # RuntimeError). The attempt is still recorded so the repo backs off instead of retrying hourly.
        obs = await _observation()

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.tasks._build_structured_llm", side_effect=exc),
        ):
            cfg.get_config.return_value = _enabled_config()
            await consolidate_memory_task.func("group/project")  # must not raise

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == ""
        assert memory.last_consolidated_at is None
        assert memory.last_attempted_at is not None, "a failed round must still back the repo off"
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    @pytest.mark.parametrize(
        ("override_model", "expected_model"),
        [
            # Site override set → used as the primary model.
            ("openrouter:anthropic/claude-opus-4.6", "openrouter:anthropic/claude-opus-4.6"),
            # Empty override → reuse the repo agent model (config.models.agent.model).
            (None, "openrouter:anthropic/claude-sonnet-4.6"),
        ],
        ids=["site_override", "empty_reuses_repo_agent"],
    )
    async def test_model_selection(self, override_model, expected_model):
        await _observation()

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.tasks._build_structured_llm", return_value=_structured_llm_returning()) as build,
            patch("memory.tasks.site_settings", _site_settings(memory_consolidation_model_name=override_model)),
        ):
            cfg.get_config.return_value = _enabled_config()
            await consolidate_memory_task.func("group/project")

        _schema, models = build.call_args.args
        assert models[0] == expected_model

    async def test_prompt_carries_entry_and_observation_ids(self):
        # The model can only target an entry by copying its ID back, so both lists must reach it.
        entry = await _entry("a fact already known")
        obs = await _observation(content="a fresh candidate observation")
        llm = _structured_llm_returning()

        await _run_round(llm)

        (messages,), _ = llm.with_config.return_value.ainvoke.call_args
        human_content = messages[-1].content
        assert str(entry.pk) in human_content
        assert "a fact already known" in human_content
        assert str(obs.pk) in human_content
        assert "a fresh candidate observation" in human_content


def test_task_default_constants_mirror_site_settings_defaults(monkeypatch):
    # The module constants are the documented defaults; they must equal the values the
    # site-settings layer serves so behavior is identical whether or not an admin overrode them.
    # Clear the env overrides too: site_settings checks them before the DB/default, so a stray
    # DAIV_MEMORY_* in the runner's environment would otherwise short-circuit this assertion.
    from unittest.mock import patch as _patch

    from memory.tasks import CONSOLIDATION_MAX_PENDING_AGE_DAYS

    from core.models import SiteConfiguration
    from core.site_settings import site_settings

    for env_var in (
        "DAIV_MEMORY_MAX_LINES",
        "DAIV_MEMORY_MAX_BYTES",
        "DAIV_MEMORY_CONSOLIDATION_MIN_PENDING",
        "DAIV_MEMORY_CONSOLIDATION_MAX_PENDING_AGE_DAYS",
    ):
        monkeypatch.delenv(env_var, raising=False)

    with _patch.object(SiteConfiguration, "get_cached", return_value=None):
        assert site_settings.memory_max_lines == MEMORY_MAX_LINES
        assert site_settings.memory_max_bytes == MEMORY_MAX_BYTES
        assert site_settings.memory_consolidation_min_pending == CONSOLIDATION_MIN_PENDING
        assert site_settings.memory_consolidation_max_pending_age_days == CONSOLIDATION_MAX_PENDING_AGE_DAYS
