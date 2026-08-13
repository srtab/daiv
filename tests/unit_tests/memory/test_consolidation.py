import logging
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest
from memory.consolidation import run_consolidation_round
from memory.models import (
    EntryStatus,
    MemoryEntry,
    MemoryObservation,
    ObservationCategory,
    ObservationStatus,
    RepositoryMemory,
)
from memory.schemas import CONTENT_HARD_LIMIT, MAX_OPERATIONS, MemoryOperation

from tests.unit_tests.memory.consolidation_helpers import (
    _enabled_config,
    _entry,
    _observation,
    _site_settings,
    _structured_llm_returning,
)


async def _run_round(llm, config=None, **settings):
    """Run one round over every pending observation for the fixture repo."""
    observations = [
        obs async for obs in MemoryObservation.objects.filter(repo_id="group/project").pending().order_by("created_at")
    ]
    with (
        patch("memory.consolidation.build_structured_llm", return_value=llm),
        patch("memory.consolidation.site_settings", _site_settings(**settings)),
    ):
        return await run_consolidation_round("group/project", config or _enabled_config(), observations)


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

        await _run_round(llm, memory_max_bytes=120)

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

    async def test_content_over_the_prompt_guideline_is_stored_verbatim(self):
        # The prompt's ~500 characters is guidance; prune_to_budget owns the document's real size.
        obs = await _observation()
        content = "x" * 900
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content=content)
        )

        outcome = await _run_round(llm)

        assert outcome.applied == 1
        entry = await MemoryEntry.objects.filter(repo_id="group/project").active().aget()
        assert entry.content == content

    async def test_runaway_content_rejects_only_its_own_operation(self):
        kept_obs = await _observation(content="a fact worth keeping")
        runaway_obs = await _observation(content="the fact the model got verbose about")
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(kept_obs.pk)], category="pitfall", content="a kept fact"),
            MemoryOperation(
                op="ADD",
                observation_ids=[str(runaway_obs.pk)],
                category="codebase_fact",
                content="x" * (CONTENT_HARD_LIMIT + 1),
            ),
        )

        outcome = await _run_round(llm)

        assert (outcome.applied, outcome.rejected) == (1, 1)
        assert [entry.content async for entry in MemoryEntry.objects.filter(repo_id="group/project").active()] == [
            "a kept fact"
        ]
        await kept_obs.arefresh_from_db()
        await runaway_obs.arefresh_from_db()
        assert kept_obs.status == ObservationStatus.CONSOLIDATED
        assert runaway_obs.status == ObservationStatus.PENDING

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
            MemoryOperation(op="UPDATE", entry_ids=["nope"], observation_ids=[str(obs.pk)], content="a revised fact")
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
async def test_accept_reports_rejected_count_without_writing():
    from memory.consolidation import ConsolidationRound

    entry = await _entry("an existing fact")
    obs = await _observation()
    # ``bad`` gets its own in-round observation so the unknown-entry check is what rejects it;
    # reusing ``obs`` would trip the earlier claimed-observation check instead.
    other_obs = await _observation(content="a second candidate observation")
    good = MemoryOperation(op="CONFIRM", entry_ids=[str(entry.pk)], observation_ids=[str(obs.pk)])
    bad = MemoryOperation(
        op="UPDATE",
        entry_ids=["00000000-0000-0000-0000-000000000000"],
        observation_ids=[str(other_obs.pk)],
        content="a revised fact",
    )

    round_ = ConsolidationRound("group/project", [good, bad], [obs, other_obs], [entry])
    accepted, rejected = round_.accept()

    assert accepted == [good]
    assert rejected == 1
    assert round_.claimed == {str(obs.pk)}
    assert round_.claimed_entries == {str(entry.pk)}
    assert await MemoryEntry.objects.acount() == 1, "accept() must not write"
    for observation in (obs, other_obs):
        await observation.arefresh_from_db()
        assert observation.status == ObservationStatus.PENDING, "accept() must not flip observation status"


@pytest.mark.django_db(transaction=True)
async def test_accept_re_decides_from_the_snapshot_on_a_second_call():
    # The claim sets are instance state: without a reset, a second call finds every observation
    # already claimed and rejects the whole round.
    from memory.consolidation import ConsolidationRound

    entry = await _entry("an existing fact")
    obs = await _observation()
    operation = MemoryOperation(op="CONFIRM", entry_ids=[str(entry.pk)], observation_ids=[str(obs.pk)])

    round_ = ConsolidationRound("group/project", [operation], [obs], [entry])

    assert round_.accept() == ([operation], 0)
    assert round_.accept() == ([operation], 0)
    assert round_.claimed == {str(obs.pk)}
    assert round_.claimed_entries == {str(entry.pk)}


async def _observations(count):
    return [await _observation(content=f"a fact learned about thing {i}") for i in range(count)]


def _adds_for(observations):
    return [
        MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content=f"a consolidated fact {i}")
        for i, obs in enumerate(observations)
    ]


@pytest.mark.django_db(transaction=True)
async def test_operations_over_the_cap_are_truncated_not_discarded(caplog):
    observations = await _observations(MAX_OPERATIONS + 3)

    with caplog.at_level(logging.WARNING, logger="daiv.memory"):
        outcome = await _run_round(_structured_llm_returning(*_adds_for(observations)))

    assert outcome.applied == MAX_OPERATIONS
    assert (outcome.still_pending, outcome.truncated) == (3, 3)
    assert f"over the {MAX_OPERATIONS} cap" in caplog.text
    assert await MemoryObservation.objects.filter(status=ObservationStatus.PENDING).acount() == 3


@pytest.mark.django_db(transaction=True)
async def test_the_truncated_tail_is_consolidated_by_the_next_round():
    # The convergence claim behind truncating rather than failing the parse: leaving the tail
    # pending is only safe if a later round actually picks it up.
    observations = await _observations(MAX_OPERATIONS + 3)

    first = await _run_round(_structured_llm_returning(*_adds_for(observations)))
    tail = [obs async for obs in MemoryObservation.objects.filter(status=ObservationStatus.PENDING)]
    second = await _run_round(_structured_llm_returning(*_adds_for(tail)))

    assert (first.truncated, second.truncated) == (3, 0)
    assert (second.applied, second.still_pending) == (3, 0)
    assert await MemoryObservation.objects.filter(status=ObservationStatus.PENDING).acount() == 0
    assert await MemoryEntry.objects.filter(repo_id="group/project").active().acount() == MAX_OPERATIONS + 3


@pytest.mark.django_db(transaction=True)
async def test_a_truncated_operation_naming_an_unknown_observation_is_not_counted_as_deferred():
    # truncated is subtracted from still_pending to decide whether the model skipped anything, so
    # counting an id that was never in the round would drive that difference negative.
    observations = await _observations(MAX_OPERATIONS)
    hallucinated = MemoryOperation(
        op="ADD", observation_ids=["not-a-real-id"], category="pitfall", content="a hallucinated fact"
    )

    outcome = await _run_round(_structured_llm_returning(*_adds_for(observations), hallucinated))

    assert (outcome.applied, outcome.still_pending, outcome.truncated) == (MAX_OPERATIONS, 0, 0)
