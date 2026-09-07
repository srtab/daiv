"""Scored quality eval for repository-memory consolidation.

Cases are seeded as real rows and run end-to-end through ``run_consolidation_round``, so the eval
exercises operation validation, claim bookkeeping, the transaction, budget pruning and the render
— not just the model call. The primary gate is deterministic: the per-observation decision is
reconstructed from what the round actually persisted and compared to the case's expectation. The
judge grades only the resulting content.

``django_db(transaction=True)`` is mandatory: under the plain marker the conftest appends, an
async ORM write followed by ``sync_to_async(round_.apply)`` fails with "database table is locked"
on the in-memory sqlite test DB. The decorator's marker wins over the conftest's appended one.
"""

import json
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from langsmith import testing as t
from memory.consolidation import run_consolidation_round
from memory.models import EntryStatus, MemoryEntry, MemoryObservation, ObservationStatus

from .memory_grading import (
    assert_no_few_shot_leak,
    decision_violations,
    duplicate_bullets,
    judge_claims,
    judge_duplicate_facts,
    record_votes,
    validate_consolidation_case,
)
from .utils import EVAL_REPEATS, MEMORY_CONSOLIDATION_MODELS, require_provider_for_model

DATA_DIR = Path(__file__).parent / "data" / "memory" / "consolidation"
TEST_SUITE = "DAIV: Memory Consolidation"


def load_cases(*, multi_round: bool):
    for line in (DATA_DIR / "cases.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        validate_consolidation_case(case)
        batches = case.get("batches") or [case.get("observations", [])]
        expect = case.get("expect", {})
        # content_must_state and final.must_state are consolidation's real graded assertions
        # (e.g. 021's must_state) — entries/observations alone miss them.
        assert_no_few_shot_leak([
            *(row["content"] for row in [*case.get("entries", []), *(row for batch in batches for row in batch)]),
            *expect.get("content_must_state", {}).values(),
            *expect.get("final", {}).get("must_state", []),
        ])
        if ("batches" in case) is multi_round:
            yield pytest.param(case, id=case["id"])


def repo_id_for(case_id: str, model_name: str, repetition: int) -> str:
    """A repo_id unique per case, model and repetition.

    ``transaction=True`` truncates between tests, not between the repetitions inside one — without
    this the second attempt would consolidate against the first attempt's entries.
    """
    return f"eval/{case_id}/{model_name}/{repetition}"


@sync_to_async
def seed_entries(repo_id: str, rows: list[dict]) -> dict[str, str]:
    """Create the case's entries, backdated, and map symbolic ids to primary keys."""
    now = timezone.now()
    return {
        row["id"]: str(
            MemoryEntry.objects.create(
                repo_id=repo_id,
                category=row["category"],
                content=row["content"],
                created_at=now - timedelta(days=row.get("created_days_ago", 0)),
                last_confirmed_at=now - timedelta(days=row.get("confirmed_days_ago", 0)),
            ).pk
        )
        for row in rows
    }


@sync_to_async
def seed_observations(repo_id: str, rows: list[dict]) -> tuple[dict[str, str], list[MemoryObservation]]:
    """Create the batch's observations, backdated, ordered oldest-first as production passes them.

    ``created_at`` is ``auto_now_add``, so it can only be backdated by a follow-up UPDATE.
    """
    now = timezone.now()
    mapping: dict[str, str] = {}
    for row in rows:
        observation = MemoryObservation.objects.create(
            repo_id=repo_id, category=row["category"], content=row["content"]
        )
        updated = MemoryObservation.objects.filter(pk=observation.pk).update(
            created_at=now - timedelta(days=row.get("created_days_ago", 0))
        )
        # A silent 0 here would flatten this row's chronology to "now" without failing anything.
        assert updated == 1, f"backdating {row['id']} touched {updated} row(s), expected 1"
        mapping[row["id"]] = str(observation.pk)
    observations = list(MemoryObservation.objects.filter(repo_id=repo_id).pending().order_by("created_at"))
    return mapping, observations


@sync_to_async
def applied_decisions(
    observation_pks: dict[str, str], entry_pks: dict[str, str]
) -> tuple[dict[str, dict], dict[str, str]]:
    """Reconstruct ``observation -> operation`` from what the round persisted.

    ``RoundOutcome`` carries counts, not operations, so the decision is read back out of the rows.
    That makes the check stronger, not weaker: it asserts the writes actually happened.
    """
    entry_symbol = {pk: symbol for symbol, pk in entry_pks.items()}
    decisions: dict[str, dict] = {}
    contents: dict[str, str] = {}

    for symbol, pk in observation_pks.items():
        observation = MemoryObservation.objects.prefetch_related("entries").get(pk=pk)
        if observation.status == ObservationStatus.PENDING:
            continue
        if observation.status == ObservationStatus.DISCARDED:
            decisions[symbol] = {"op": "DISCARD", "entries": set(), "operation_key": f"discard:{symbol}"}
            continue

        linked = list(observation.entries.all())
        if not linked:
            continue
        entry = linked[-1]
        if str(entry.pk) in entry_symbol:
            decisions[symbol] = {
                "op": "CONFIRM",
                "entries": {entry_symbol[str(entry.pk)]},
                "operation_key": str(entry.pk),
            }
            continue

        superseded = [
            entry_symbol[str(previous.pk)]
            for previous in MemoryEntry.objects.filter(superseded_by=entry)
            if str(previous.pk) in entry_symbol
        ]
        op = {0: "ADD", 1: "UPDATE"}.get(len(superseded), "MERGE")
        decisions[symbol] = {"op": op, "entries": set(superseded), "operation_key": str(entry.pk)}
        contents[symbol] = entry.content

    return decisions, contents


@sync_to_async
def active_entries(repo_id: str) -> list[str]:
    return [
        entry.content
        for entry in MemoryEntry.objects.filter(repo_id=repo_id, status=EntryStatus.ACTIVE).order_by("created_at")
    ]


@sync_to_async
def rendered_document(repo_id: str) -> str:
    """The stored render — what an agent run would actually be shown."""
    from memory.models import RepositoryMemory

    return RepositoryMemory.objects.filter(repo_id=repo_id).values_list("content", flat=True).first() or ""


async def _attempt(case: dict, model_name: str, repetition: int) -> tuple[bool, str, dict]:
    """One graded consolidation round. Returns (passed, why-not, what-happened)."""
    repo_id = repo_id_for(case["id"], model_name, repetition)
    entry_pks = await seed_entries(repo_id, case.get("entries", []))
    observation_pks, observations = await seed_observations(repo_id, case["observations"])

    outcome = await run_consolidation_round(repo_id, observations, model_names=[model_name])
    decisions, contents = await applied_decisions(observation_pks, entry_pks)
    evidence = {"outcome": None if outcome is None else vars(outcome), "decisions": decisions, "contents": contents}

    if outcome is None:
        return False, "the round applied nothing", evidence
    if violations := decision_violations(decisions, case["expect"]):
        return False, "; ".join(violations), evidence

    statements = case["expect"].get("content_must_state", {})
    graded = {symbol: text for symbol, text in statements.items() if symbol in contents}
    if not graded:
        return True, "", evidence

    claims = [f"the entry produced for {symbol} states {text}" for symbol, text in graded.items()]
    emitted = [f"{symbol}: {contents[symbol]}" for symbol in graded]
    verdicts, errors = await judge_claims(emitted, claims, what="the entries a consolidation round produced")
    if errors:
        return False, "; ".join(errors), evidence

    unmet = [claim for claim in claims if not verdicts[claim]]
    return not unmet, "; ".join(f"content does not state: {claim!r}" for claim in unmet), evidence


@pytest.mark.memory
@pytest.mark.django_db(transaction=True)
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", MEMORY_CONSOLIDATION_MODELS)
@pytest.mark.parametrize("case", load_cases(multi_round=False))
async def test_memory_consolidation(case, model_name):
    require_provider_for_model(model_name)
    t.log_inputs({"case": case["id"], "entries": case.get("entries", []), "observations": case["observations"]})

    results: list[bool] = []
    details: list[str] = []
    evidence: list[dict] = []
    try:
        for repetition in range(EVAL_REPEATS):
            try:
                passed, detail, what_happened = await _attempt(case, model_name, repetition)
            except Exception as exc:  # noqa: BLE001 — a crashed attempt must still vote FAIL, not vanish
                passed, detail = False, f"attempt {repetition} raised {exc!r}"
                what_happened = {"outcome": None, "decisions": {}, "contents": {}}
            results.append(passed)
            details.append(detail)
            evidence.append(what_happened)
    finally:
        # In a finally so a cell that never finishes still leaves a FAIL row in votes_report
        # instead of silently vanishing from Task 10's only source for BASELINE.md.
        record_votes(TEST_SUITE, f"{case['id']}[{model_name}]", results)
        t.log_outputs({"votes": results, "evidence": evidence})

    report = "\n".join(
        f"  attempt {index}: {detail or 'ok'} | {what_happened['decisions']}"
        for index, (detail, what_happened) in enumerate(zip(details, evidence, strict=True), start=1)
    )
    assert sum(results) * 2 > EVAL_REPEATS, (
        f"{case['id']} @ {model_name}: {sum(results)}/{EVAL_REPEATS} attempts passed\n{report}"
    )


async def _attempt_multi_round(case: dict, model_name: str, repetition: int) -> tuple[bool, str, dict]:
    """Run every batch against accumulated state, then grade the final entry set.

    Each batch is a separate round. ``seed_observations`` re-reads what is pending, so an
    observation an earlier round left unclaimed is re-fed to the next one — which is exactly what
    production does.
    """
    repo_id = repo_id_for(case["id"], model_name, repetition)
    await seed_entries(repo_id, case.get("entries", []))

    rounds: list[str] = []
    for number, batch in enumerate(case["batches"], start=1):
        _mapping, observations = await seed_observations(repo_id, batch)
        outcome = await run_consolidation_round(repo_id, observations, model_names=[model_name])
        rounds.append(f"round {number}: {'nothing applied' if outcome is None else vars(outcome)}")

    document = await rendered_document(repo_id)
    contents = await active_entries(repo_id)
    evidence = {"rounds": rounds, "entries": contents, "document": document}
    final = case["expect"]["final"]
    problems: list[str] = []

    if (cap := final.get("max_entries")) is not None and len(contents) > cap:
        problems.append(f"ended with {len(contents)} entries, over the cap of {cap}")

    if final.get("no_duplicate_facts"):
        # Deterministic and free first: an exact repeat needs no model call.
        if repeats := duplicate_bullets(document):
            problems.append(f"the document repeats bullet(s) verbatim: {repeats}")
        elif document.strip():
            # An empty document has no two bullets to compare — skip a call whose answer is
            # already known, rather than asking the judge to grade nothing.
            duplicated, explanation = await judge_duplicate_facts(document)
            if duplicated:
                problems.append(f"two bullets state the same fact: {explanation}")

    if claims := final.get("must_state", []):
        verdicts, errors = await judge_claims(contents, claims, what="the entries a repository's memory ended up with")
        if errors:
            problems.extend(errors)
        else:
            problems.extend(f"the final entries do not state: {claim!r}" for claim in claims if not verdicts[claim])

    return not problems, "; ".join(problems), evidence


@pytest.mark.memory
@pytest.mark.django_db(transaction=True)
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", MEMORY_CONSOLIDATION_MODELS)
@pytest.mark.parametrize("case", load_cases(multi_round=True))
async def test_memory_consolidation_converges(case, model_name):
    require_provider_for_model(model_name)
    t.log_inputs({"case": case["id"], "batches": case["batches"]})

    results: list[bool] = []
    details: list[str] = []
    evidence: list[dict] = []
    try:
        for repetition in range(EVAL_REPEATS):
            try:
                passed, detail, what_happened = await _attempt_multi_round(case, model_name, repetition)
            except Exception as exc:  # noqa: BLE001 — a crashed attempt must still vote FAIL, not vanish
                passed, detail = False, f"attempt {repetition} raised {exc!r}"
                what_happened = {"rounds": [], "entries": [], "document": ""}
            results.append(passed)
            details.append(detail)
            evidence.append(what_happened)
    finally:
        # In a finally so a cell that never finishes still leaves a FAIL row in votes_report
        # instead of silently vanishing from Task 10's only source for BASELINE.md.
        record_votes(TEST_SUITE, f"{case['id']}[{model_name}]", results)
        t.log_outputs({"votes": results, "evidence": evidence})

    report = "\n".join(
        f"  attempt {index}: {detail or 'ok'}\n    document:\n{what_happened['document']}"
        for index, (detail, what_happened) in enumerate(zip(details, evidence, strict=True), start=1)
    )
    assert sum(results) * 2 > EVAL_REPEATS, (
        f"{case['id']} @ {model_name}: {sum(results)}/{EVAL_REPEATS} attempts passed\n{report}"
    )
